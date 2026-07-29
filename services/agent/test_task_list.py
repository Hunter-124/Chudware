"""Tests for the task-list planning surface (services/agent/task_list.py +
services/mcp/task_list_tools.py + config.get_task_list_preference).

What these defend, concretely:
  * The host upserts plan_state items BY ID, so an id that shifts between
    revisions duplicates the row instead of updating it.
  * The general lane's live sink is a contextvar; with no sink installed
    (offline / unit-test / non-GUI dispatch) publish must be a silent no-op,
    never a crash inside the turn it was called from.
  * task_list is a planning tool the model calls first — if it raises on a
    dialect it does not like, it aborts the very turn it exists to organize.
"""
from __future__ import annotations

import unittest

from services.agent import config, task_list
from services.mcp.task_list_tools import register_task_list_tools


def _tool():
    """The raw task_list callable, resolved the way agent dispatch resolves it."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("test-tasklist")
    register_task_list_tools(mcp)
    return mcp._tool_manager._tools["task_list"].fn


class _Sink:
    """(kind, payload) live sink, the arity agent_turn installs."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))


class TaskListStoreTest(unittest.TestCase):
    RUN = "test-run-store"

    def setUp(self):
        task_list.reset(self.RUN)

    tearDown = setUp

    def test_ids_are_stable_across_revisions(self):
        first = task_list.publish(self.RUN, ["Sketch base", "Extrude", "Save"])
        second = task_list.publish(self.RUN, [
            {"title": "Sketch base", "status": "done"},
            {"title": "Extrude", "status": "in_progress"},
            {"title": "Save", "status": "todo"},
        ])
        self.assertEqual([i["id"] for i in first["items"]],
                         ["task:1", "task:2", "task:3"])
        self.assertEqual([i["id"] for i in second["items"]],
                         [i["id"] for i in first["items"]])

    def test_caller_supplied_ids_win(self):
        snap = task_list.publish(self.RUN, [{"id": "step-a", "title": "Do a"}])
        self.assertEqual(snap["items"][0]["id"], "step-a")

    def test_duplicate_ids_are_rekeyed_not_dropped(self):
        snap = task_list.publish(self.RUN, [
            {"id": "x", "title": "First"}, {"id": "x", "title": "Second"}])
        ids = [i["id"] for i in snap["items"]]
        self.assertEqual(len(snap["items"]), 2)
        self.assertEqual(len(set(ids)), 2)

    def test_revision_increments_per_publish(self):
        self.assertEqual(task_list.publish(self.RUN, ["a"])["revision"], 1)
        self.assertEqual(task_list.publish(self.RUN, ["a"])["revision"], 2)
        self.assertEqual(task_list.publish(self.RUN, ["a"])["revision"], 3)
        task_list.reset(self.RUN)
        self.assertEqual(task_list.publish(self.RUN, ["a"])["revision"], 1)

    def test_revisions_are_per_run(self):
        task_list.publish(self.RUN, ["a"])
        other = task_list.publish(self.RUN + "-2", ["a"])
        try:
            self.assertEqual(other["revision"], 1)
        finally:
            task_list.reset(self.RUN + "-2")

    def test_string_items_normalize(self):
        item = task_list.publish(self.RUN, ["  Trim   the   sketch  "])["items"][0]
        self.assertEqual(item["title"], "Trim the sketch")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["kind"], "task")
        self.assertNotIn("phase", item)   # optional fields stay absent
        self.assertNotIn("desc", item)

    def test_dict_item_title_aliases(self):
        for key in ("title", "name", "text", "task", "step", "label"):
            with self.subTest(key=key):
                snap = task_list.publish(self.RUN, [{key: "Mill the slot"}])
                self.assertEqual(snap["items"][0]["title"], "Mill the slot")

    def test_status_aliases_normalize(self):
        cases = {
            "todo": "pending", "open": "pending", "not_started": "pending",
            "pending": "pending",
            "in_progress": "running", "in progress": "running",
            "active": "running", "doing": "running", "started": "running",
            "running": "running",
            "complete": "done", "completed": "done", "finished": "done",
            "ok": "done", "passed": "done", "done": "done",
            "pass": "pass",
            "failed": "fail", "error": "fail", "fail": "fail",
            "skipped": "abandoned", "cancelled": "abandoned",
            "dropped": "abandoned", "abandoned": "abandoned",
        }
        for raw, want in cases.items():
            with self.subTest(status=raw):
                snap = task_list.publish(self.RUN, [{"title": "t", "status": raw}])
                self.assertEqual(snap["items"][0]["status"], want)

    def test_unknown_status_falls_back_to_pending(self):
        snap = task_list.publish(self.RUN, [{"title": "t", "status": "banana"}])
        self.assertEqual(snap["items"][0]["status"], "pending")

    def test_optional_fields_only_when_supplied(self):
        item = task_list.publish(self.RUN, [
            {"title": "Weld", "desc": "TIG, 3mm", "phase": "assembly"},
        ])["items"][0]
        self.assertEqual(item["desc"], "TIG, 3mm")
        self.assertEqual(item["phase"], "assembly")

    def test_bounds(self):
        snap = task_list.publish(
            self.RUN,
            [{"title": "x" * 500, "desc": "y" * 900}] + ["item"] * 100,
            title="T" * 500, summary="S" * 900,
        )
        self.assertEqual(len(snap["items"]), task_list.MAX_ITEMS)
        self.assertLessEqual(len(snap["items"][0]["title"]), task_list.MAX_TITLE_CHARS)
        self.assertLessEqual(len(snap["items"][0]["desc"]), task_list.MAX_DESC_CHARS)
        self.assertLessEqual(len(snap["title"]), task_list.MAX_TITLE_CHARS)
        self.assertLessEqual(len(snap["summary"]), task_list.MAX_SUMMARY_CHARS)

    def test_run_store_is_bounded(self):
        for n in range(task_list.MAX_RUNS + 8):
            task_list.publish(f"bound-run-{n}", ["a"])
        self.assertLessEqual(len(task_list._RUNS), task_list.MAX_RUNS)
        self.assertIsNone(task_list.snapshot("bound-run-0"))
        self.assertIsNotNone(
            task_list.snapshot(f"bound-run-{task_list.MAX_RUNS + 7}"))

    def test_snapshot_roundtrip_and_reset(self):
        task_list.publish(self.RUN, ["a"], title="Plan")
        self.assertEqual(task_list.snapshot(self.RUN)["title"], "Plan")
        task_list.reset(self.RUN)
        self.assertIsNone(task_list.snapshot(self.RUN))

    def test_shape_normalizes_to_contract_vocabulary(self):
        self.assertEqual(
            task_list.publish(self.RUN, ["a"], shape="Phased-Todo")["shape"],
            "phased_todo")
        self.assertEqual(
            task_list.publish(self.RUN, ["a"], shape="nonsense")["shape"],
            "checklist")


class PlanStateEventTest(unittest.TestCase):
    RUN = "test-run-events"

    def setUp(self):
        task_list.reset(self.RUN)

    tearDown = setUp

    def test_event_reaches_installed_sink_with_contract_fields(self):
        sink = _Sink()
        token = task_list.set_event_sink(sink)
        try:
            task_list.publish(self.RUN, ["Sketch", {"title": "Extrude",
                                                    "status": "running"}],
                              title="Bracket build", summary="2 of 3")
        finally:
            task_list.clear_event_sink(token)
        self.assertEqual(len(sink.events), 1)
        kind, payload = sink.events[0]
        self.assertEqual(kind, "plan_state")
        for field in ("shape", "title", "revision", "items"):
            self.assertIn(field, payload)
        self.assertEqual(payload["shape"], "checklist")
        self.assertEqual(payload["title"], "Bracket build")
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["summary"], "2 of 3")
        self.assertEqual([i["status"] for i in payload["items"]],
                         ["pending", "running"])
        for item in payload["items"]:
            self.assertEqual(item["kind"], "task")
            self.assertIn(item["status"], task_list.VALID_STATUSES)

    def test_single_dict_sink_arity_is_supported(self):
        seen: list[dict] = []
        token = task_list.set_event_sink(lambda data: seen.append(data))
        try:
            task_list.publish(self.RUN, ["a"])
        finally:
            task_list.clear_event_sink(token)
        self.assertEqual(seen[0]["kind"], "plan_state")

    def test_no_sink_is_a_silent_noop(self):
        # Offline / unit dispatch: publishing must still store and return.
        snap = task_list.publish(self.RUN, ["a"])
        self.assertEqual(snap["revision"], 1)

    def test_raising_sink_does_not_break_publish(self):
        def boom(kind, payload):
            raise RuntimeError("flaky UI sink")

        token = task_list.set_event_sink(boom)
        try:
            snap = task_list.publish(self.RUN, ["a"])
        finally:
            task_list.clear_event_sink(token)
        self.assertEqual(snap["revision"], 1)

    def test_sink_is_cleared_after_the_turn(self):
        sink = _Sink()
        task_list.clear_event_sink(task_list.set_event_sink(sink))
        task_list.publish(self.RUN, ["a"])
        self.assertEqual(sink.events, [])


class TaskListToolTest(unittest.TestCase):
    RUN = "test-run-tool"

    def setUp(self):
        self.fn = _tool()
        task_list.reset(self.RUN)

    def tearDown(self):
        task_list.reset(self.RUN)

    def test_happy_path_envelope(self):
        out = self.fn(items=[{"title": "Sketch", "status": "done"},
                             {"title": "Extrude", "status": "running"},
                             "Save"],
                      title="Bracket", run_id=self.RUN)
        self.assertTrue(out["ok"])
        self.assertEqual((out["revision"], out["total"], out["done"]), (1, 3, 1))
        self.assertEqual([i["id"] for i in out["items"]],
                         ["task:1", "task:2", "task:3"])

    def test_items_kwarg_aliases(self):
        for alias in ("tasks", "todos", "steps", "plan", "list"):
            with self.subTest(alias=alias):
                task_list.reset(self.RUN)
                out = self.fn(run_id=self.RUN, **{alias: ["a", "b"]})
                self.assertTrue(out["ok"], out)
                self.assertEqual(out["total"], 2)

    def test_json_string_items_are_accepted(self):
        out = self.fn(items='[{"title": "Sketch", "status": "todo"}]',
                      run_id=self.RUN)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["items"][0]["status"], "pending")

    def test_newline_block_items_are_accepted(self):
        out = self.fn(items="- Sketch\n- Extrude\n", run_id=self.RUN)
        self.assertTrue(out["ok"], out)
        self.assertEqual([i["title"] for i in out["items"]], ["Sketch", "Extrude"])

    def test_garbage_returns_error_envelope_not_raise(self):
        for bad in (42, None, "", [], {}, object()):
            with self.subTest(bad=repr(bad)):
                out = self.fn(items=bad, run_id=self.RUN)
                self.assertFalse(out["ok"], out)
                self.assertIn("hint", out)
                self.assertIn("task_list(", out["hint"])

    def test_scalar_list_entries_are_salvaged_not_rejected(self):
        # A bare number is a numbered step, not a reason to fail the call;
        # None entries are dropped.
        out = self.fn(items=[123, None, "Save"], run_id=self.RUN)
        self.assertTrue(out["ok"], out)
        self.assertEqual([i["title"] for i in out["items"]], ["123", "Save"])

    def test_unknown_kwargs_do_not_raise(self):
        # Models invent kwargs; a TypeError here would kill the whole turn.
        out = self.fn(items=["a"], run_id=self.RUN, priority="high", foo=1)
        self.assertTrue(out["ok"], out)

    def test_tool_emits_plan_state(self):
        sink = _Sink()
        token = task_list.set_event_sink(sink)
        try:
            self.fn(items=["a", "b"], title="Plan", run_id=self.RUN)
        finally:
            task_list.clear_event_sink(token)
        self.assertEqual([k for k, _ in sink.events], ["plan_state"])

    def test_resolves_through_agent_dispatch(self):
        from services.agent.reviewer_exec import _resolve_fn

        self.assertTrue(callable(_resolve_fn("task_list")))


class TaskListPreferenceTest(unittest.TestCase):
    def test_default_is_encourage(self):
        self.assertEqual(config.DEFAULT_TASK_LIST_PREFERENCE, "encourage")
        self.assertEqual(config.get_task_list_preference({}), "encourage")

    def test_top_level_key(self):
        self.assertEqual(
            config.get_task_list_preference({"task_list_preference": "off"}), "off")

    def test_agent_block_key_wins(self):
        cfg = {"task_list_preference": "off",
               "agent": {"task_list_preference": "require"}}
        self.assertEqual(config.get_task_list_preference(cfg), "require")

    def test_gui_int_values(self):
        for n, want in enumerate(("off", "option", "encourage", "require")):
            with self.subTest(n=n):
                self.assertEqual(
                    config.get_task_list_preference({"task_list_preference": n}), want)

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(
            config.get_task_list_preference({"task_list_preference": "banana"}),
            "encourage")


if __name__ == "__main__":
    unittest.main()
