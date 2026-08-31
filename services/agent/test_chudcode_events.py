from __future__ import annotations

import copy
import json
import unittest

from services.agent.chudcode_events import map_rpc_frame

CTX = {
    "run_id": "run-7",
    "session_id": "session-2",
    "node_id": "root-node",
    "actor": "general_agent",
    "parent_id": "root-span",
    "root_agent_id": "root-agent",
}


def one(frame, context=CTX):
    events = map_rpc_frame(frame, context)
    assert len(events) == 1
    return events[0]


class ChudcodeEventMapperTests(unittest.TestCase):
    def test_agent_turn_and_message_lifecycle_do_not_repeat_final_text(self):
        kind, start = one(
            {"type": "agent_start", "agentId": "root-agent", "model": "gpt-x"}
        )
        self.assertEqual(kind, "run_started")
        self.assertEqual(start["run_id"], "run-7")
        self.assertEqual(start["node_id"], "root-agent")
        self.assertEqual(start["model"], "gpt-x")

        for frame, expected_phase in [
            ({"type": "turn_start", "turnId": "turn-1"}, "turn_start"),
            (
                {"type": "message_start", "message": {"id": "m1", "role": "assistant"}},
                "message_start",
            ),
            (
                {
                    "type": "message_end",
                    "message": {"id": "m1", "content": "already streamed"},
                },
                "message_end",
            ),
            (
                {"type": "turn_end", "message": {"content": "already streamed"}},
                "turn_end",
            ),
            ({"type": "prompt_result", "content": "already streamed"}, "prompt_result"),
        ]:
            out_kind, payload = one(frame)
            self.assertEqual(out_kind, "status")
            self.assertEqual(payload["phase"], expected_phase)
            self.assertNotIn("already streamed", json.dumps(payload))
        self.assertEqual(
            one({"type": "message_end", "message": {"id": "m1", "content": "done"}})[1][
                "id"
            ],
            "m1",
        )

    def test_message_update_unwraps_assistant_event_and_preserves_message_id(self):
        kind, payload = one(
            {
                "type": "message_update",
                "message": {"id": "message-9", "parentId": "turn-span"},
                "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
            }
        )
        self.assertEqual(
            (kind, payload["channel"], payload["text"]),
            ("assistant_delta", "text", "hello"),
        )
        self.assertEqual(payload["id"], "message-9")
        self.assertEqual(payload["parent_id"], "turn-span")

    def test_delta_channel_switches_tails_and_hidden_flushes_stay_exact(self):
        fixtures = [
            ({"type": "thinking_delta", "text": "reason "}, "thinking", "reason "),
            ({"type": "text_delta", "text": "answer"}, "text", "answer"),
            ({"type": "text_delta", "text": ""}, "text", ""),
            ({"type": "thinking_delta", "text": " tail"}, "thinking", " tail"),
            (
                {"type": "assistant_delta", "channel": "analysis", "text": "x"},
                "thinking",
                "x",
            ),
            (
                {"type": "assistant_delta", "channel": "text", "text": "\n"},
                "text",
                "\n",
            ),
        ]
        mapped = [one(frame) for frame, _, _ in fixtures]
        self.assertEqual(
            [(payload["channel"], payload["text"]) for _, payload in mapped],
            [(channel, text) for _, channel, text in fixtures],
        )
        self.assertTrue(all(kind == "assistant_delta" for kind, _ in mapped))

    def test_builtin_tool_lifecycle_nesting_and_result_shape(self):
        kind, start = one(
            {
                "type": "tool_execution_start",
                "toolCallId": "call-child",
                "parentToolCallId": "call-parent",
                "toolName": "render_part",
                "args": {"name": "housing"},
            }
        )
        self.assertEqual(kind, "status")
        self.assertEqual(start["phase"], "tool")
        self.assertEqual(start["id"], "call-child")
        self.assertEqual(start["parent_id"], "call-parent")

        kind, update = one(
            {
                "type": "update",
                "toolCallId": "call-child",
                "toolName": "render_part",
                "partialResult": "halfway",
            }
        )
        self.assertEqual(kind, "status")
        self.assertEqual(update["id"], "call-child")
        self.assertEqual(update["text"], "halfway")

        frame = {
            "type": "end",
            "toolCallId": "call-child",
            "parentToolCallId": "call-parent",
            "toolName": "render_part",
            "args": {"name": "housing"},
            "result": {"path": "/tmp/housing.png"},
            "isError": False,
            "durationMs": 17,
            "executed": True,
            "artifacts": [{"path": "/tmp/housing.png", "kind": "image"}],
            "usage": {"inputTokens": 12, "contextTokens": 20, "cacheReadTokens": 8},
        }
        kind, end = one(frame)
        self.assertEqual(kind, "agent.tool_call")
        self.assertEqual(end["id"], "call-child")
        self.assertEqual(end["parent_id"], "call-parent")
        self.assertEqual(end["tool_name"], "render_part")
        self.assertEqual(end["target"], "housing")
        self.assertTrue(end["ok"])
        self.assertEqual(end["latency_ms"], 17)
        self.assertEqual(
            end["artifacts"], [{"path": "/tmp/housing.png", "kind": "image"}]
        )
        self.assertEqual(end["usage"]["cache_read"], 8)
        self.assertEqual(len(end["args_digest"]), 16)
        self.assertEqual(len(end["result_digest"]), 16)

    def test_host_tool_complete_and_cancel_use_final_tool_card(self):
        kind, complete = one(
            {
                "type": "host_tool_call",
                "id": "host-1",
                "toolName": "measure_part",
                "args": {"doc_id": "doc-cpp1"},
                "result": {"ok": True},
                "isError": False,
            }
        )
        self.assertEqual(kind, "agent.tool_call")
        self.assertTrue(complete["ok"])
        self.assertEqual(complete["id"], "host-1")

        kind, cancelled = one(
            {
                "type": "host_tool_cancel",
                "toolCallId": "host-2",
                "toolName": "render_part",
                "executed": True,
            }
        )
        self.assertEqual(kind, "agent.tool_call")
        self.assertFalse(cancelled["ok"])
        self.assertEqual(cancelled["error_preview"], "cancelled")
        self.assertTrue(cancelled["executed"])

    def test_todos_map_to_scoped_full_snapshot(self):
        kind, plan = one(
            {
                "type": "todo_update",
                "revision": 3,
                "title": "Work",
                "todos": [
                    {"id": "a", "content": "Sketch profile", "status": "in_progress"},
                    {
                        "id": "b",
                        "title": "Extrude",
                        "status": "completed",
                        "parentId": "a",
                    },
                    {"id": "c", "title": "Inspect", "status": "cancelled"},
                ],
            }
        )
        self.assertEqual(kind, "plan_state")
        self.assertEqual(plan["shape"], "checklist")
        self.assertEqual(plan["revision"], 3)
        self.assertEqual(
            [item["id"] for item in plan["items"]], ["todo:a", "todo:b", "todo:c"]
        )
        self.assertEqual(
            [item["status"] for item in plan["items"]], ["running", "done", "abandoned"]
        )
        self.assertEqual(plan["items"][1]["parent_id"], "todo:a")

        kind, cleared = one({"type": "auto_clear"})
        self.assertEqual(kind, "plan_state")
        self.assertEqual(cleared["items"], [])

    def test_model_and_thinking_changes(self):
        kind, model = one(
            {"type": "succeeded", "model": "sonnet-x", "role": "reviewer"}
        )
        self.assertEqual(kind, "agent.model_assignment")
        self.assertEqual(model["picked_model"], "sonnet-x")
        self.assertEqual(model["role"], "reviewer")

        kind, thinking = one(
            {"type": "reasoning_effort_changed", "reasoningEffort": "high"}
        )
        self.assertEqual(kind, "status")
        self.assertEqual(thinking["phase"], "thinking_change")
        self.assertEqual(thinking["thinking"], "high")

    def test_compaction_retry_and_fallback_are_visible(self):
        kind, start = one(
            {
                "type": "auto_compaction_start",
                "id": "compact-1",
                "reason": "window pressure",
                "action": "snapcompact",
            }
        )
        self.assertEqual((kind, start["status"]), ("sub_run", "running"))
        self.assertEqual(start["id"], "compact-1")

        kind, end = one(
            {
                "type": "auto_compaction_end",
                "id": "compact-1",
                "action": "snapcompact",
                "result": "saved 1200",
                "aborted": False,
            }
        )
        self.assertEqual((kind, end["status"]), ("sub_run", "done"))
        self.assertEqual(end["id"], "compact-1")

        kind, retry = one(
            {
                "type": "auto_retry_start",
                "attempt": 2,
                "maxAttempts": 4,
                "delayMs": 250,
                "errorMessage": "rate limited",
                "model": "m1",
            }
        )
        self.assertEqual(kind, "provider_call")
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["max_attempts"], 4)
        self.assertFalse(retry["ok"])

        kind, retry_end = one(
            {"type": "auto_retry_end", "success": False, "finalError": "nope"}
        )
        self.assertEqual((kind, retry_end["status"]), ("sub_run", "fail"))

        kind, fallback = one(
            {
                "type": "retry_fallback_applied",
                "fromModel": "m1",
                "toModel": "m2",
                "role": "worker",
            }
        )
        self.assertEqual((kind, fallback["status"]), ("sub_run", "done"))
        self.assertIn("m2", fallback["purpose"])

    def test_subagent_lineage_spawn_progress_generic_notification_and_end(self):
        kind, spawn = one(
            {
                "type": "subagent_spawn",
                "subagentId": "child-1",
                "parentId": "spawn-span",
                "role": "modeler",
                "brief": "Build bracket",
                "budgetTokens": 9000,
            }
        )
        self.assertEqual(kind, "agent.spawn")
        self.assertEqual(spawn["subagent_id"], "child-1")
        self.assertEqual(spawn["parent_id"], "spawn-span")
        self.assertEqual(spawn["budget_tokens"], 9000)

        kind, progress = one(
            {
                "type": "subagent_progress",
                "subagentId": "child-1",
                "parentId": "spawn-span",
                "purpose": "Build bracket",
                "progress": 0.6,
                "sessionFile": "/tmp/child.jsonl",
            }
        )
        self.assertEqual((kind, progress["status"]), ("sub_run", "running"))
        self.assertEqual(progress["subagent_id"], "child-1")
        self.assertEqual(progress["parent_id"], "spawn-span")
        self.assertEqual(progress["session_file"], "/tmp/child.jsonl")

        kind, generic = one(
            {
                "type": "future_subagent_notice",
                "subagentId": "child-1",
                "sessionFile": "/tmp/child.jsonl",
                "status": "blocked",
                "message": "Need input",
            }
        )
        self.assertEqual(kind, "sub_run")
        self.assertEqual(generic["status"], "blocked")

        kind, end = one(
            {"type": "subagent_end", "subagentId": "child-1", "status": "failed"}
        )
        self.assertEqual((kind, end["status"]), ("sub_run", "fail"))

    def test_usage_and_context_shapes_and_order(self):
        mapped = map_rpc_frame(
            {
                "type": "usage_update",
                "provider": "openai",
                "model": "gpt-x",
                "inputTokens": 10,
                "outputTokens": 4,
                "contextTokens": 25,
                "cacheReadTokens": 15,
            },
            CTX,
        )
        self.assertEqual(
            [kind for kind, _ in mapped], ["agent.provider_usage", "agent.context"]
        )
        self.assertEqual(mapped[0][1]["total_tokens"], 14)
        self.assertEqual(mapped[1][1]["cache_read"], 15)

        kind, injected = one(
            {
                "type": "context_injected",
                "source": "memory",
                "label": "Project notes",
                "tokenEst": 80,
            }
        )
        self.assertEqual(kind, "agent.context_injected")
        self.assertEqual(injected["token_est"], 80)

        kind, window = one(
            {"type": "context_window", "contextTokens": 100, "contextWindow": 200}
        )
        self.assertEqual(kind, "agent.context")
        self.assertEqual(window["context_window"], 200)

    def test_extension_ui_command_output_and_errors(self):
        kind, request = one(
            {
                "type": "extension_ui_request",
                "id": "ui-1",
                "title": "Choose face",
                "request": {"choices": list(range(100))},
            }
        )
        self.assertEqual((kind, request["status"]), ("sub_run", "waiting"))
        self.assertEqual(request["id"], "ui-1")
        self.assertLessEqual(len(request["request"]["choices"]), 33)

        kind, output = one(
            {
                "type": "command_output",
                "commandId": "cmd-1",
                "command": "python build.py",
                "stream": "stdout",
                "output": "line",
                "done": False,
            }
        )
        self.assertEqual((kind, output["status"]), ("sub_run", "running"))
        self.assertEqual(output["id"], "cmd-1")
        self.assertEqual(output["detail"], "line")

        kind, ext_error = one(
            {"type": "extension_error", "id": "ui-1", "error": "closed"}
        )
        self.assertEqual((kind, ext_error["status"]), ("sub_run", "fail"))

        kind, nonterminal = one({"type": "protocol_error", "message": "bad frame"})
        self.assertEqual((kind, nonterminal["status"]), ("sub_run", "fail"))

        terminal = map_rpc_frame(
            {
                "type": "provider_error",
                "isTerminal": True,
                "errorMessage": "auth failed",
                "stopReason": "provider_auth",
            },
            CTX,
        )
        self.assertEqual([kind for kind, _ in terminal], ["status", "run_finished"])
        self.assertEqual(terminal[0][1]["phase"], "error")
        self.assertEqual(terminal[1][1]["error_detail"], "auth failed")

    def test_agent_end_terminal_and_nonterminal_semantics(self):
        kind, terminal = one(
            {
                "type": "agent_end",
                "status": "completed",
                "messageCount": 3,
                "messages": [{"content": "must not display again"}],
            }
        )
        self.assertEqual(kind, "run_finished")
        self.assertEqual(terminal["status"], "done")
        self.assertNotIn("must not display again", json.dumps(terminal))

        kind, nonterminal = one(
            {
                "type": "agent_end",
                "isTerminal": False,
                "agentId": "maintenance",
                "status": "completed",
                "purpose": "Compaction maintenance",
            }
        )
        self.assertEqual(kind, "sub_run")
        self.assertEqual(nonterminal["status"], "done")
        self.assertNotEqual(kind, "run_finished")
        kind, child_end = one(
            {
                "type": "agent_end",
                "subagentId": "child-9",
                "status": "completed",
            }
        )
        self.assertEqual((kind, child_end["status"]), ("sub_run", "done"))
        self.assertEqual(child_end["subagent_id"], "child-9")

    def test_unknown_events_are_transparent_bounded_and_deterministic(self):
        frame = {
            "type": "available_commands_update",
            "status": "completed",
            "commands": [{"name": "x" * 5000} for _ in range(100)],
            "huge": "z" * 10000,
        }
        original = copy.deepcopy(frame)
        first = map_rpc_frame(frame, CTX)
        second = map_rpc_frame(copy.deepcopy(frame), copy.deepcopy(CTX))
        self.assertEqual(first, second)
        self.assertEqual(frame, original)
        kind, payload = first[0]
        self.assertEqual(kind, "sub_run")
        self.assertEqual(payload["rpc_event"], "available_commands_update")
        self.assertEqual(payload["status"], "done")
        self.assertLessEqual(len(payload["detail"]), 4096)
        self.assertTrue(payload["id"].startswith("rpc-"))

        invalid_kind, invalid = one(["not", "a", "frame"])
        self.assertEqual(invalid_kind, "sub_run")
        self.assertEqual(invalid["rpc_event"], "invalid_frame")
        mixed_kind, mixed = one({"type": "future_event", "odd": {1: "a", "1": "b"}})
        self.assertEqual(mixed_kind, "sub_run")
        self.assertLessEqual(len(mixed["detail"]), 4096)

    def test_raw_wrapper_tolerance_and_context_are_not_mutated(self):
        frame = {"method": "event", "params": {"type": "text_delta", "text": "x"}}
        frame_before = copy.deepcopy(frame)
        context = copy.deepcopy(CTX)
        context_before = copy.deepcopy(context)
        self.assertEqual(one(frame, context)[1]["text"], "x")
        self.assertEqual(frame, frame_before)
        self.assertEqual(context, context_before)


if __name__ == "__main__":
    unittest.main()
