from __future__ import annotations

import copy
import json
import unittest

from services.agent.chudcode_events import RpcFrameState, map_rpc_frame

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
        ]:
            out_kind, payload = one(frame)
            self.assertEqual(out_kind, "status")
            self.assertEqual(payload["phase"], expected_phase)
            self.assertNotIn("already streamed", json.dumps(payload))
        for frame in (
            {"type": "prompt_result", "id": "req_1", "agentInvoked": True,
             "status": "completed", "sessionSettled": True},
            {"type": "session_settled"},
            {"type": "queue_update", "steering": ["change axis"], "followUp": []},
        ):
            self.assertEqual(map_rpc_frame(frame, CTX), [])
        self.assertEqual(
            one({"type": "message_end", "message": {"id": "m1", "content": "done"}})[1][
                "id"
            ],
            "m1",
        )
        self.assertEqual(
            one({"type": "message_update", "messageId": "msg-new",
                 "assistantMessageEvent": {"type": "text_delta", "delta": "now"}})[1]["id"],
            "msg-new",
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

    def test_failed_builtin_reports_its_own_reason(self):
        # Chudcode puts a builtin failure's reason ONLY in result.content; the card said
        # "tool failed" for an unknown tool and for calls a queued steer skipped.
        for reason in (
            "Tool xd___recall not found",
            "Skipped due to queued user message. Do not count this skipped result.",
        ):
            _, end = one(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-x",
                    "toolName": "read",
                    "result": {"content": [{"type": "text", "text": reason}], "details": {}},
                    "isError": True,
                }
            )
            self.assertFalse(end["ok"])
            self.assertEqual(end["error_preview"], reason)

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

    def test_unkeyed_compaction_frames_pair_into_one_row_per_compaction(self):
        """Chudcode's real frames carry no id; the end must close its start row."""
        state = RpcFrameState()

        def mapped(frame):
            [(kind, row)] = map_rpc_frame(frame, CTX, state)
            self.assertEqual(kind, "sub_run")
            return row

        start = mapped(
            {"type": "auto_compaction_start", "reason": "threshold", "action": "context-full"}
        )
        end = mapped(
            {
                "type": "auto_compaction_end",
                "action": "context-full",
                "result": {"summary": "s", "tokensBefore": 274850},
                "aborted": False,
                "willRetry": False,
            }
        )
        self.assertEqual((start["status"], end["status"]), ("running", "done"))
        self.assertEqual(start["id"], end["id"])
        self.assertIn("compacting", start["purpose"].lower())
        second = mapped(
            {"type": "auto_compaction_start", "reason": "overflow", "action": "context-full"}
        )
        failed = mapped(
            {
                "type": "auto_compaction_end",
                "action": "context-full",
                "result": None,
                "aborted": False,
                "willRetry": False,
                "errorMessage": "summary request failed",
            }
        )
        self.assertNotEqual(second["id"], start["id"])
        self.assertEqual((second["id"], failed["status"]), (failed["id"], "fail"))
        self.assertEqual(failed["detail"], "summary request failed")

    def test_restarted_response_is_named_not_an_unknown_agent_row(self):
        """A second assistant ``start`` is a provider-stream restart, not a mystery row."""
        out = map_rpc_frame(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "start", "partial": {"content": []}},
                "message": {"role": "assistant"},
            },
            CTX,
            RpcFrameState(),
        )
        self.assertEqual([kind for kind, _ in out], ["sub_run", "status"])
        self.assertEqual(out[0][1]["status"], "retry")
        self.assertNotIn("Agent \u00b7", out[0][1]["purpose"])

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

    def test_task_subagent_frames_nest_tool_calls_under_the_task_call(self):
        """Chudcode's subscription frames for a `task` sub-agent: one upserted
        sub_run per sub-agent under the spawning task call, its CAD tool calls
        as agent.tool_call rows carrying subagent_id/parent_id, and none of its
        private text reaching the parent's reply."""
        state = RpcFrameState()

        def mapped(frame):
            return map_rpc_frame(frame, CTX, state)

        def nested(event):
            return {"type": "subagent_event", "payload": {"id": "CubeBuilder", "event": event}}

        [(kind, started)] = mapped(
            {
                "type": "subagent_lifecycle",
                "payload": {
                    "id": "CubeBuilder",
                    "agent": "task",
                    "agentSource": "bundled",
                    "description": "Build the 10 mm cube",
                    "status": "started",
                    "parentToolCallId": "toolu_task_1",
                    "index": 0,
                },
            }
        )
        self.assertEqual(kind, "sub_run")
        self.assertEqual(
            (started["id"], started["subagent_id"], started["parent_id"], started["status"]),
            ("CubeBuilder", "CubeBuilder", "toolu_task_1", "running"),
        )
        self.assertEqual(started["purpose"], "Build the 10 mm cube")

        call = {
            "toolCallId": "toolu_cube_1",
            "toolName": "write",
            "args": {"path": "xd://new_part", "content": '{"name": "cube"}'},
        }
        mapped(nested({"type": "tool_execution_start", **call}))
        self.assertEqual(
            mapped(
                nested(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "Cube done"},
                    }
                )
            ),
            [],
        )
        [(kind, tool_call)] = mapped(
            nested(
                {
                    "type": "tool_execution_end",
                    "toolCallId": "toolu_cube_1",
                    "toolName": "new_part",
                    "result": {"content": [{"type": "text", "text": "{}"}], "details": {"ok": True}},
                    "isError": False,
                }
            )
        )
        self.assertEqual(kind, "agent.tool_call")
        self.assertEqual(
            (tool_call["id"], tool_call["tool_name"], tool_call["ok"]),
            ("toolu_cube_1", "new_part", True),
        )
        self.assertEqual(
            (tool_call["subagent_id"], tool_call["parent_id"]), ("CubeBuilder", "toolu_task_1")
        )

        [(kind, progress)] = mapped(
            {
                "type": "subagent_progress",
                "payload": {
                    "index": 0,
                    "agent": "task",
                    "task": "Build the 10 mm cube",
                    "parentToolCallId": "toolu_task_1",
                    "progress": {"id": "CubeBuilder", "status": "running", "description": "Build the 10 mm cube"},
                },
            }
        )
        self.assertEqual((kind, progress["id"], progress["status"]), ("sub_run", "CubeBuilder", "running"))

        [(kind, ended)] = mapped(
            {
                "type": "subagent_lifecycle",
                "payload": {
                    "id": "CubeBuilder",
                    "agent": "task",
                    "status": "completed",
                    "parentToolCallId": "toolu_task_1",
                    "index": 0,
                },
            }
        )
        self.assertEqual((kind, ended["id"], ended["status"]), ("sub_run", "CubeBuilder", "done"))

    def test_task_subagent_rows_carry_the_assigned_name_not_the_agent_type(self):
        """Chudcode's real frame shapes: the lifecycle start has no description
        (the tiny-model label lands later in progress) and `agent` is the
        definition type. The row name is the spawner-assigned id on every frame,
        nested ids included, and never degrades to "task"."""
        state = RpcFrameState()

        def row(frame):
            [(kind, body)] = map_rpc_frame(frame, CTX, state)
            self.assertEqual(kind, "sub_run")
            return body

        lifecycle = {
            "id": "HubTaskNames",
            "index": 0,
            "agent": "task",
            "agentSource": "bundled",
            "sessionFile": "/tmp/HubTaskNames.jsonl",
            "parentToolCallId": "toolu_task_1",
        }
        started = row({"type": "subagent_lifecycle", "payload": {**lifecycle, "status": "started"}})
        self.assertEqual(
            (started["name"], started["purpose"], started["agent_type"]),
            ("HubTaskNames", "HubTaskNames", "task"),
        )
        self.assertNotIn("description", started)

        progress = row(
            {
                "type": "subagent_progress",
                "payload": {
                    "index": 0,
                    "agent": "task",
                    "agentSource": "bundled",
                    "task": "<rendered subagent prompt>",
                    "assignment": "# Target\nFix hub names",
                    "parentToolCallId": "toolu_task_1",
                    "progress": {
                        "index": 0,
                        "id": "HubTaskNames",
                        "agent": "task",
                        "status": "running",
                        "task": "<rendered subagent prompt>",
                        "description": "Fix subagent hub task names",
                        "lastIntent": "Reading the DAG",
                    },
                },
            }
        )
        self.assertEqual(
            (progress["name"], progress["description"], progress["purpose"]),
            ("HubTaskNames", "Fix subagent hub task names", "Fix subagent hub task names"),
        )

        nested = row(
            {
                "type": "subagent_lifecycle",
                "payload": {
                    "id": "HubTaskNames.Scout",
                    "index": 0,
                    "agent": "scout",
                    "agentSource": "bundled",
                    "status": "started",
                    "parentToolCallId": "toolu_task_2",
                },
            }
        )
        self.assertEqual(
            (nested["name"], nested["agent_type"], nested["parent_id"]),
            ("HubTaskNames.Scout", "scout", "toolu_task_2"),
        )

        terminal_progress = row(
            {
                "type": "subagent_progress",
                "payload": {
                    "index": 0,
                    "agent": "task",
                    "task": "<rendered subagent prompt>",
                    "progress": {"id": "HubTaskNames", "agent": "task", "status": "completed"},
                },
            }
        )
        self.assertEqual((terminal_progress["name"], terminal_progress["status"]), ("HubTaskNames", "done"))
        self.assertEqual(terminal_progress["parent_id"], "toolu_task_1")

        ended = row({"type": "subagent_lifecycle", "payload": {**lifecycle, "status": "completed"}})
        self.assertEqual((ended["name"], ended["purpose"], ended["status"]), ("HubTaskNames", "HubTaskNames", "done"))
        self.assertNotIn("task", (started["purpose"], ended["purpose"], terminal_progress["purpose"]))

    def test_spawn_and_generic_lifecycle_keep_name_separate_from_role(self):
        kind, spawned = one(
            {
                "type": "subagent_spawn",
                "subagentId": "opaque-child-1",
                "name": "BoreReview",
                "role": "task",
                "brief": "Review bearing bore fit",
            }
        )
        self.assertEqual((kind, spawned["name"], spawned["role"]), ("agent.spawn", "BoreReview", "task"))
        kind, ended = one(
            {
                "type": "subagent_completed",
                "subagentId": "opaque-child-1",
                "name": "BoreReview",
                "purpose": "task",
            }
        )
        self.assertEqual((kind, ended["name"], ended["status"]), ("sub_run", "BoreReview", "done"))

    def test_tool_end_surfaces_returned_images_as_path_refs(self):
        render_end = {
            "type": "tool_execution_end",
            "toolCallId": "r1",
            "toolName": "render_part",
            "result": {
                "content": [
                    {"type": "text", "text": '{"ok": true, "caption": "Iso view of bracket"}'},
                    {"type": "image", "mimeType": "image/png", "data": "iVBOR"},
                ],
                "details": {
                    "ok": True,
                    "caption": "Iso view of bracket",
                    "images": [
                        {"kind": "image", "path": "/tmp/r1.png", "mime": "image/png", "width": 800, "height": 600}
                    ],
                },
            },
        }
        kind, payload = one(render_end)
        self.assertEqual(kind, "agent.tool_call")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["artifacts"], [{"path": "/tmp/r1.png", "kind": "image"}])
        self.assertEqual(payload["image_caption"], "Iso view of bracket")
        self.assertNotIn("iVBOR", json.dumps(payload))

        kind, failed = one(
            {
                "type": "tool_execution_end",
                "toolCallId": "c1",
                "toolName": "write",
                "args": {"path": "xd://check_clearance", "content": "{}"},
                "result": {
                    "content": [{"type": "text", "text": "{}"}],
                    "details": {
                        "xdev": {
                            "inner": {
                                "ok": False,
                                "error": "clash",
                                "caption": "Section A",
                                "images": [{"kind": "image", "path": "/tmp/c.png"}],
                            }
                        }
                    },
                },
            }
        )
        self.assertFalse(failed["ok"])
        self.assertIn({"path": "/tmp/c.png", "kind": "image"}, failed["artifacts"])
        self.assertEqual(failed["image_caption"], "Section A")

        state = RpcFrameState()
        map_rpc_frame(
            {
                "type": "subagent_lifecycle",
                "payload": {"id": "Renderer", "agent": "task", "status": "started", "parentToolCallId": "toolu_task_9", "index": 0},
            },
            CTX,
            state,
        )
        [(kind, nested)] = map_rpc_frame(
            {"type": "subagent_event", "payload": {"id": "Renderer", "event": render_end}}, CTX, state
        )
        self.assertEqual(
            (kind, nested["subagent_id"], nested["parent_id"]), ("agent.tool_call", "Renderer", "toolu_task_9")
        )
        self.assertEqual(nested["artifacts"], [{"path": "/tmp/r1.png", "kind": "image"}])
        self.assertEqual(nested["image_caption"], "Iso view of bracket")

        multiple = copy.deepcopy(render_end)
        multiple["artifacts"] = [{"kind": "image", "path": "/tmp/r1.png"}]
        multiple["result"]["details"]["images"] += [
            {"kind": "image", "path": "/tmp/r2.png"},
            {"kind": "image", "path": "/tmp/r1.png"},
        ]
        _, deduped = one(multiple)
        self.assertEqual(
            deduped["artifacts"],
            [{"path": "/tmp/r1.png", "kind": "image"}, {"path": "/tmp/r2.png", "kind": "image"}],
        )
        self.assertEqual(deduped["image_caption"], "Iso view of bracket")

        already_present = copy.deepcopy(render_end)
        already_present["artifacts"] = [{"kind": "image", "path": "/tmp/r1.png"}]
        _, existing = one(already_present)
        self.assertEqual(existing["artifacts"], [{"path": "/tmp/r1.png", "kind": "image"}])
        self.assertEqual(existing["image_caption"], "Iso view of bracket")

        _, numeric = one(
            {
                "type": "tool_execution_end",
                "toolCallId": "q1",
                "toolName": "inspect_geometry",
                "result": {"details": {"ok": True, "clearance_mm": 1.25}},
            }
        )
        self.assertNotIn("artifacts", numeric)
        self.assertNotIn("image_caption", numeric)

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
        self.assertEqual(mapped[1][1]["used"], 25)

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
        self.assertEqual((window["used"], window["window"]), (100, 200))

        kind, window_only = one({"type": "context_window", "contextWindow": 200})
        self.assertNotIn("used", window_only)

    def test_assistant_message_end_reports_measured_context(self):
        usage = {"input": 2, "output": 8, "cacheRead": 287_415, "cacheWrite": 9_159}

        def context_of(message, **frame):
            mapped = map_rpc_frame({"type": "message_end", "message": message, **frame}, CTX)
            return [p for kind, p in mapped if kind == "agent.context"]

        [measured] = context_of({"role": "assistant", "stopReason": "stop", "usage": usage})
        self.assertEqual((measured["used"], measured["cache_read"]), (296_584, 287_415))
        [occupancy] = context_of({"role": "assistant", "usage": {**usage, "contextTokens": 5}})
        self.assertEqual(occupancy["used"], 5)
        # Partial usage, a nested agent's window, and user envelopes never move the meter.
        self.assertEqual(context_of({"role": "assistant", "stopReason": "error", "usage": usage}), [])
        self.assertEqual(
            context_of({"role": "assistant", "usage": usage}, subagentId="child-1"), []
        )
        self.assertEqual(context_of({"role": "user", "usage": usage}), [])

    def test_extension_ui_command_output_and_errors(self):
        kind, request = one(
            {
                "type": "extension_ui_request",
                "id": "ui-1",
                "method": "select",
                "title": "Choose face",
                "request": {"choices": list(range(100))},
            }
        )
        self.assertEqual((kind, request["status"]), ("sub_run", "fail"))
        self.assertEqual(request["id"], "ui-1")
        self.assertLessEqual(len(request["request"]["choices"]), 33)

    def test_a_ui_request_never_parks_an_unresolvable_waiting_row(self):
        """No UI-request frame may leave a row nothing can ever resolve.

        Passive methods carry information and get no terminal frame; the
        interactive four are cancelled outright by chudcode_host. A "waiting"
        row for either parks a spinner in the feed for the rest of the session,
        which is what made a finished turn look hung.
        """
        for method in (
            "notify",
            "setStatus",
            "setWidget",
            "setTitle",
            "set_editor_text",
            "open_url",
            "cancel",
            "",
        ):
            kind, row = one(
                {"type": "extension_ui_request", "id": "p", "method": method}
            )
            self.assertEqual((method, kind, row["status"]), (method, "sub_run", "done"))

        for method in ("select", "confirm", "input", "editor"):
            kind, row = one(
                {"type": "extension_ui_request", "id": "i", "method": method}
            )
            self.assertEqual((method, kind, row["status"]), (method, "sub_run", "fail"))
            self.assertIn("declined", row["detail"])

    def test_a_passive_notice_keeps_its_message(self):
        _, row = one(
            {
                "type": "extension_ui_request",
                "id": "n",
                "method": "notify",
                "message": "Signed in as hunter",
            }
        )
        self.assertEqual(row["purpose"], "Signed in as hunter")
        self.assertEqual(row["status"], "done")

    def test_command_output_and_error_frames(self):
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
                "yielded": False,
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
            "type": "future_event",
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
        self.assertEqual(payload["rpc_event"], "future_event")
        self.assertEqual(payload["status"], "done")
        self.assertLessEqual(len(payload["detail"]), 4096)
        self.assertTrue(payload["id"].startswith("rpc-"))

        invalid_kind, invalid = one(["not", "a", "frame"])
        self.assertEqual(invalid_kind, "sub_run")
        self.assertEqual(invalid["rpc_event"], "invalid_frame")
        mixed_kind, mixed = one({"type": "future_event", "odd": {1: "a", "1": "b"}})
        self.assertEqual(mixed_kind, "sub_run")
        self.assertLessEqual(len(mixed["detail"]), 4096)

    def test_recorded_custom_tool_lifecycle_retains_start_arguments_and_intent(self):
        state = RpcFrameState()
        read_url = "https://grabcad.com/library/servo-motor-mg90s-2"
        frames = [
            {
                "type": "custom",
                "customType": "tool_execution_start",
                "data": {
                    "toolCallId": "read-1",
                    "toolName": "read",
                    "args": {"path": read_url},
                    "intent": "Reading MG90S STEP model",
                },
                "id": "envelope-1",
                "parentId": "assistant-1",
            },
            {
                "type": "custom_message",
                "customType": "tool_execution_start",
                "data": {
                    "toolCallId": "search-1",
                    "toolName": "web_search",
                    "args": {"query": "MG90S STEP model"},
                    "intent": "Finding servo reference",
                },
            },
            {
                "type": "custom",
                "customType": "tool_execution_update",
                "data": {"toolCallId": "read-1", "partialResult": "Fetching"},
            },
            {
                "type": "custom",
                "customType": "tool_execution_end",
                "data": {"toolCallId": "read-1", "toolName": "read", "result": "page"},
            },
            {
                "type": "custom",
                "customType": "tool_execution_end",
                "data": {
                    "toolCallId": "search-1",
                    "toolName": "web_search",
                    "result": "results",
                },
            },
            {
                "type": "custom",
                "customType": "tool_execution_end",
                "data": {"toolCallId": "read-1", "toolName": "read", "result": "page"},
            },
        ]
        mapped = [row for frame in frames for row in map_rpc_frame(frame, CTX, state)]
        self.assertEqual([kind for kind, _ in mapped], ["status"] * 3 + ["agent.tool_call"] * 2)
        self.assertEqual(mapped[0][1]["id"], "read-1")
        self.assertEqual(mapped[0][1]["parent_id"], "assistant-1")
        read_result, search_result = (row for kind, row in mapped if kind == "agent.tool_call")
        self.assertEqual(read_result["target"], read_url)
        self.assertIn(read_url, read_result["args_preview"])
        self.assertEqual(read_result["intent"], "Reading MG90S STEP model")
        self.assertEqual(search_result["target"], "MG90S STEP model")
        self.assertEqual(search_result["intent"], "Finding servo reference")
        self.assertEqual(read_result["id"], "read-1")

    def test_toolcall_boundary_supplies_query_when_custom_start_omits_args(self):
        state = RpcFrameState()
        self.assertEqual(
            map_rpc_frame(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "toolcall_end",
                        "toolCall": {
                            "id": "search-2",
                            "name": "web_search",
                            "arguments": {"query": "MG90S mounting dimensions"},
                        },
                    },
                },
                CTX,
                state,
            ),
            [],
        )
        self.assertEqual(
            map_rpc_frame(
                {
                    "type": "custom",
                    "customType": "tool_execution_start",
                    "data": {
                        "toolCallId": "search-2",
                        "toolName": "web_search",
                        "intent": "Finding servo mounting dimensions",
                    },
                },
                CTX,
                state,
            )[0][0],
            "status",
        )
        kind, row = map_rpc_frame(
            {
                "type": "custom",
                "customType": "tool_execution_end",
                "data": {"toolCallId": "search-2", "result": "results"},
            },
            CTX,
            state,
        )[0]
        self.assertEqual(kind, "agent.tool_call")
        self.assertEqual(row["target"], "MG90S mounting dimensions")
        self.assertIn("MG90S mounting dimensions", row["args_preview"])
        self.assertEqual(row["intent"], "Finding servo mounting dimensions")

    def test_tool_targets_prefer_semantic_arguments_and_bound_large_previews(self):
        state = RpcFrameState()
        for call_id, tool, args, expected in [
            ("fs-1", "fs_read", {"name": "untitled", "path": "parts/servo.step"}, "parts/servo.step"),
            ("cad-1", "new_part", {"name": "Servo bracket", "path": "parts/"}, "Servo bracket"),
            ("research-1", "research_parts", {"name": "other", "query": "MG90S horn"}, "MG90S horn"),
        ]:
            self.assertEqual(
                map_rpc_frame(
                    {"type": "tool_execution_start", "toolCallId": call_id, "toolName": tool, "args": args},
                    CTX,
                    state,
                )[0][0],
                "status",
            )
            kind, row = map_rpc_frame(
                {"type": "tool_execution_end", "toolCallId": call_id, "toolName": tool, "result": {}},
                CTX,
                state,
            )[0]
            self.assertEqual(kind, "agent.tool_call")
            self.assertEqual(row["target"], expected)
        map_rpc_frame(
            {"type": "tool_execution_start", "toolCallId": "large", "toolName": "new_part", "args": {"name": "x" * 10000}},
            CTX,
            state,
        )
        _, row = map_rpc_frame(
            {"type": "tool_execution_end", "toolCallId": "large", "result": {}},
            CTX,
            state,
        )[0]
        self.assertLessEqual(len(row["args_preview"]), 2048)
        self.assertLessEqual(len(row["target"]), 120)

    def test_xd_host_calls_show_inner_arguments_not_the_write_wrapper(self):
        state = RpcFrameState()
        for call_id, tool, arguments, target in [
            ("search", "part_search", {"query": "MG90S", "limit": 5}, "MG90S"),
            ("standard", "make_standard_part", {"family": "hobby_servo", "size": "MG90S"}, "hobby_servo · MG90S"),
            ("source", "fetch_cad_model", {"url": "https://example.org/servo.step"}, "https://example.org/servo.step"),
            ("feature", "inspect_feature", {"feature": "Servo mount"}, "Servo mount"),
        ]:
            raw_args = {"path": f"xd://{tool}", "content": json.dumps(arguments)}
            map_rpc_frame(
                {"type": "tool_execution_start", "toolCallId": call_id, "toolName": tool, "args": raw_args},
                CTX,
                state,
            )
            kind, row = map_rpc_frame(
                {"type": "tool_execution_end", "toolCallId": call_id, "toolName": tool, "result": {"ok": True}},
                CTX,
                state,
            )[0]
            self.assertEqual(kind, "agent.tool_call")
            self.assertEqual(row["target"], target)
            self.assertEqual(json.loads(row["args_preview"]), arguments)
            self.assertNotIn("xd://", row["args_preview"])

        kind, docs = one(
            {
                "type": "tool_execution_end",
                "toolCallId": "docs",
                "toolName": "read",
                "args": {"path": "xd://part_search"},
            }
        )
        self.assertEqual(kind, "agent.tool_call")
        self.assertEqual(docs["target"], "docs")
        self.assertNotIn("args_preview", docs)
        kind, invalid = one(
            {
                "type": "tool_execution_end",
                "toolCallId": "bad-json",
                "toolName": "write",
                "args": {"path": "xd://part_search", "content": "{broken"},
            }
        )
        self.assertEqual(kind, "agent.tool_call")
        self.assertEqual(invalid["target"], "")
        self.assertNotIn("args_preview", invalid)

    def test_a_cad_ok_false_result_is_a_failed_card_not_a_success(self):
        """Chudcode reports a CAD {"ok": false} return with isError unset; under
        `write xd://` it sits in details.xdev.inner. The card must fail with the
        tool's own reason and outcome, direct or via the device."""
        refusal = {"ok": False, "status": "guardrail_blocked", "policy_refusal": True,
                   "executed": False, "error": "HARD guardrail: dangling refs m3"}
        failure = {"ok": False, "error": "fillet: 4 mm is too large for the selected edge(s)"}
        cases = [
            ({"content": [{"type": "text", "text": "{}"}],
              "details": {"xdev": {"tool": "add_features", "mode": "execute", "inner": refusal}}},
             "HARD guardrail: dangling refs m3", "guard_block", False),
            ({"content": [{"type": "text", "text": "{}"}], "details": failure},
             failure["error"], "tool_error", None),
        ]
        for result, error, outcome, executed in cases:
            _, row = one({"type": "tool_execution_end", "toolCallId": "c", "toolName": "write",
                          "args": {"path": "xd://add_features", "content": "{}"}, "result": result})
            self.assertIs(row["ok"], False)
            self.assertEqual(row["error_preview"], error)
            self.assertEqual(row["outcome_kind"], outcome)
            self.assertEqual(row.get("executed"), executed)
        _, ok_row = one({"type": "tool_execution_end", "toolCallId": "d", "toolName": "write",
                         "args": {"path": "xd://add_features", "content": "{}"},
                         "result": {"details": {"xdev": {"inner": {"ok": True}}}}})
        self.assertIs(ok_row["ok"], True)

    def test_known_metadata_and_notices_do_not_produce_completed_subruns(self):
        for frame in [
            {"type": "thinking_level_change", "thinkingLevel": "xhigh"},
            {"type": "thinking_level_changed", "thinkingLevel": "high"},
            {"type": "title_change", "title": "Design 6-axis arm"},
            {"type": "notice", "text": "Session signed in"},
            {"type": "agent.progress_nudge", "message": "Proceed to CAD"},
            {"type": "session_exit", "data": {"reason": "dispose"}},
        ]:
            kind, payload = one(frame)
            self.assertEqual(kind, "status", frame["type"])
            self.assertNotIn("Agent ·", json.dumps(payload))
        self.assertEqual(one({"type": "thinking_level_change", "thinkingLevel": "xhigh"})[1]["thinking"], "xhigh")
        self.assertEqual(one({"type": "title_change", "title": "Design 6-axis arm"})[1]["title"], "Design 6-axis arm")
        self.assertEqual(one({"type": "notice", "text": "Session signed in"})[1]["text"], "Session signed in")
        self.assertEqual(one({"type": "agent.progress_nudge", "message": "Proceed to CAD"})[1]["text"], "Proceed to CAD")
        for frame in [
            {"type": "available_commands_update", "commands": [{"name": "read"}]},
            {"type": "notice"},
            {"type": "credential_pin", "provider": "anthropic", "hash": "secret"},
            {"type": "custom_message", "customType": "eager-todo-prelude", "content": "hidden"},
        ]:
            self.assertEqual(map_rpc_frame(frame, CTX), [], frame["type"])

    def test_content_block_boundaries_emit_no_row(self):
        # Chudcode brackets every assistant content block with start/end frames and
        # streams a tool call's arguments as toolcall_delta. All of that is already
        # carried by the deltas and by tool_execution_*; mapping them to sub_run rows
        # buried the reply under dozens of "Chudcode · toolcall delta" cards.
        for frame in [
            {"type": "text_start", "contentIndex": 0},
            {"type": "text_end", "contentIndex": 0, "content": "the final answer"},
            {"type": "thinking_start", "contentIndex": 1},
            {"type": "thinking_end", "contentIndex": 1, "content": "private reasoning"},
            {"type": "toolcall_start", "contentIndex": 2},
            {"type": "toolcall_delta", "contentIndex": 2, "delta": '{"path":"a'},
            {
                "type": "toolcall_end",
                "contentIndex": 2,
                "toolCall": {"id": "call-1", "name": "read"},
            },
        ]:
            self.assertEqual(map_rpc_frame(frame, CTX), [], frame["type"])
        # A message_update wrapper carrying one of them is equally silent.
        self.assertEqual(
            map_rpc_frame(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_end", "content": "done"},
                    "message": {"id": "m5", "role": "assistant"},
                },
                CTX,
            ),
            [],
        )
        # The deltas they bracket still render, and a genuinely unknown frame still
        # falls back to a bounded sub_run row.
        self.assertEqual(one({"type": "text_delta", "delta": "hi"})[0], "assistant_delta")
        self.assertEqual(one({"type": "brand_new_frame"})[0], "sub_run")

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


def test_question_reply_acknowledges_only_its_correlated_user_message():
    events = map_rpc_frame({"type": "agent_answer", "batch_id": "b-mount",
                            "source": "reply", "reply": "Use command strips",
                            "steer_id": "s-mount"}, CTX)
    assert [kind for kind, _ in events] == ["agent.answer", "agent.steer"]
    assert events[1][1]["steer_id"] == "s-mount"
    assert events[1][1]["status"] == "delivered"
    assert events[1][1]["run_id"] == CTX["run_id"]
    assert events[1][1]["session_id"] == CTX["session_id"]
    for source in ("user", "declined", "cancelled"):
        assert [kind for kind, _ in map_rpc_frame(
            {"type": "agent_answer", "batch_id": "b-mount", "source": source,
             "steer_id": "s-mount"}, CTX)] == ["agent.answer"]
    assert [kind for kind, _ in map_rpc_frame(
        {"type": "agent_answer", "batch_id": "b-mount", "source": "reply",
         "reply": "legacy uncorrelated text"}, CTX)] == ["agent.answer"]
