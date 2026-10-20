"""Tests for agent.tool_options + design_recipe + tool_hints."""
from __future__ import annotations

import threading
import unittest
from unittest import mock


class TestGetToolOptions(unittest.TestCase):
    def test_defaults_when_missing(self):
        from services.agent.config import get_tool_options

        # A name with neither a config entry nor curated guidance → stock defaults.
        opts = get_tool_options({}, "no_such_tool_zzz")
        self.assertTrue(opts["enabled"])
        self.assertIsNone(opts["timeout_s"])
        self.assertIsNone(opts["max_calls_per_turn"])
        self.assertEqual(opts["param_defaults"], {})
        self.assertEqual(opts["prompt_note"], "")
        self.assertEqual(opts["description_override"], "")
        self.assertIsNone(opts["schema_trim"])

    def test_unknown_keys_ignored(self):
        from services.agent.config import get_tool_options

        cfg = {"agent": {"tool_options": {"x": {"enabled": False, "nope": 1}}}}
        opts = get_tool_options(cfg, "x")
        self.assertFalse(opts["enabled"])
        self.assertNotIn("nope", opts)

    def test_param_defaults_and_note(self):
        from services.agent.config import get_tool_options

        cfg = {
            "agent": {
                "tool_options": {
                    "research_web_search": {
                        "param_defaults": {"max_results": 5},
                        "prompt_note": "Keep it short.",
                        "timeout_s": 30,
                        "max_calls_per_turn": 3,
                        "description_override": "Custom search blurb.",
                        "schema_trim": False,
                    }
                }
            }
        }
        opts = get_tool_options(cfg, "research_web_search")
        self.assertEqual(opts["param_defaults"], {"max_results": 5})
        self.assertEqual(opts["prompt_note"], "Keep it short.")
        self.assertEqual(opts["timeout_s"], 30)
        self.assertEqual(opts["max_calls_per_turn"], 3)
        self.assertEqual(opts["description_override"], "Custom search blurb.")
        self.assertIs(opts["schema_trim"], False)


class TestCuratedGuidanceLayer(unittest.TestCase):
    """Built-in curated guidance (tool_guidance) fills fields the user's
    config.json entry leaves unset — the user always wins per-field."""

    def _opts(self, cfg, name, curated):
        from services.agent.config import get_tool_options

        with mock.patch(
            "services.agent.tool_guidance.guidance_for", return_value=curated
        ):
            return get_tool_options(cfg, name)

    def test_curated_applies_when_no_user_entry(self):
        opts = self._opts({}, "t", {"prompt_note": "Curated.", "timeout_s": 60})
        self.assertEqual(opts["prompt_note"], "Curated.")
        self.assertEqual(opts["timeout_s"], 60)

    def test_user_wins_per_field(self):
        cfg = {"agent": {"tool_options": {"t": {"prompt_note": "User."}}}}
        opts = self._opts(cfg, "t", {"prompt_note": "Curated.", "timeout_s": 60})
        self.assertEqual(opts["prompt_note"], "User.")
        self.assertEqual(opts["timeout_s"], 60)  # curated field survives

    def test_user_can_silence_a_curated_note(self):
        cfg = {"agent": {"tool_options": {"t": {"prompt_note": ""}}}}
        opts = self._opts(cfg, "t", {"prompt_note": "Curated."})
        self.assertEqual(opts["prompt_note"], "")

    def test_real_curated_note_renders_in_tools_block(self):
        # ledger_get ships a curated note (tool_guidance.ledger) — it must reach
        # the tools slot with NO config entry at all.
        from services.agent import tool_schemas as ts

        def dummy(run_id: str, section: str | None = None, budget_tokens: int = 800):
            """Return the design ledger."""
            return {}

        text, _ = ts.render_tools_block(
            ["ledger_get"],
            resolver=lambda n: dummy if n == "ledger_get" else None,
            budget_tokens=2000,
            include_category_menu=False,
        )
        self.assertIn("Note:", text)


class TestDenyReasonEnabled(unittest.TestCase):
    def test_disabled_tool_refused(self):
        from services.mcp import tool_registry as reg

        with mock.patch(
            "services.agent.config.get_tool_options",
            side_effect=lambda c, n: {
                "enabled": False,
                "timeout_s": None,
                "max_calls_per_turn": None,
                "param_defaults": {},
                "prompt_note": "",
            },
        ):
            reason = reg.deny_reason("run_fea_static")
        self.assertIsNotNone(reason)
        self.assertIn("disabled", reason.lower())

    def test_design_recipe_always_allowed(self):
        from services.mcp import tool_registry as reg

        self.assertIsNone(reg.deny_reason("design_recipe"))


class TestDispatchOptions(unittest.TestCase):
    def test_param_defaults_merge_call_wins(self):
        from services.agent import agent_turn as at

        seen = {}

        def fake_fn(**kwargs):
            seen.update(kwargs)
            return {"ok": True}

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {"a": 1, "b": 2},
            "prompt_note": "",
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ), mock.patch(
            "services.agent.reviewer_exec._resolve_fn", return_value=fake_fn
        ):
            at._dispatch_tool("t", {"b": 9, "c": 3})
        self.assertEqual(seen, {"a": 1, "b": 9, "c": 3})

    def test_mission_scoped_handler_obeys_disabled_option(self):
        from services.agent import agent_turn as at

        called = []

        class _Mission:
            @staticmethod
            def resolve(name):
                if name == "mission_tool":
                    return lambda **kwargs: called.append(kwargs) or {"ok": True}
                return None

        class _Provider:
            name = "scripted"

            @staticmethod
            def complete(system, messages, max_tokens=8000):
                return '{"tool":"mission_tool","args":{}}'

        opts = {
            "enabled": False,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {},
            "prompt_note": "",
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=opts
        ):
            result = at.run_turn(
                "run it",
                _Provider(),
                allowed_tools=["mission_tool"],
                max_tool_calls=1,
                mission_dispatch=_Mission(),
            )

        self.assertEqual(called, [])
        self.assertFalse(result.result["calls"][0]["ok"])
        self.assertIn("disabled", str(result.result["calls"][0]["result"]).lower())

    def test_mission_scoped_handler_receives_effective_defaults(self):
        from services.agent import agent_turn as at

        seen = {}

        class _Mission:
            @staticmethod
            def resolve(name):
                if name == "office_read_csv":
                    return lambda **kwargs: seen.update(kwargs) or {"ok": True}
                return None

        class _Provider:
            name = "scripted"

            def __init__(self):
                self.replies = [
                    '{"tool":"office_read_csv","args":{"path":"bom.csv"}}',
                    '{"done":true,"handoff":{"summary":"read"}}',
                ]

            def complete(self, system, messages, max_tokens=8000):
                return self.replies.pop(0)

        opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {"limit": 500},
            "prompt_note": "",
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=opts
        ):
            at.run_turn(
                "read the csv",
                _Provider(),
                allowed_tools=["office_read_csv"],
                mission_dispatch=_Mission(),
            )

        self.assertEqual(seen, {"path": "bom.csv", "limit": 500})

    def test_max_calls_per_turn(self):
        """The cap REFUSES as data, names the remedy, and is charged per call.

        Live 2026-08-26: a research turn got a red "failed" card reading
        ``tool 'research_web_search' exceeded max_calls_per_turn=3
        (agent.tool_options)`` — a config key where a next action belonged.
        """
        from services.agent import agent_turn as at

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": 1,
            "param_defaults": {},
            "prompt_note": "Use research_fetch_page to read what this found.",
        }
        counts: dict = {}
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ), mock.patch(
            "services.agent.reviewer_exec._resolve_fn",
            return_value=lambda **k: {"ok": True},
        ):
            first = at._dispatch_tool("t", {}, _call_counts=counts)
            second = at._dispatch_tool("t", {}, _call_counts=counts)
        # The call that spends the last unit warns BEFORE the cap bites.
        self.assertIn("BUDGET", first.get("arg_interpretation", ""))
        # The refusal is DATA, carries the remedy, and is flagged as policy.
        self.assertIs(second["ok"], False)
        self.assertEqual(second["status"], "call_cap_reached")
        self.assertIs(second["policy_refusal"], True)
        self.assertEqual(second["call_cap"], 1)
        self.assertIn("research_fetch_page", second["error"])
        self.assertTrue(at._is_policy_refusal(second))

    def test_cap_is_not_charged_for_calls_that_never_ran(self):
        """Argument-validation teaching errors must not retire the tool.

        Charging the cap BEFORE dispatch meant N malformed calls permanently
        capped a tool the model had never once used successfully — and the
        refusal then blamed a limit it had not actually spent.
        """
        from services.agent import agent_turn as at

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": 2,
            "param_defaults": {},
            "prompt_note": "",
        }
        counts: dict = {}
        bad = {"ok": False, "status": "bad_arguments", "error": "missing path"}
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ), mock.patch(
            "services.agent.reviewer_exec._resolve_fn",
            return_value=lambda **k: bad,
        ):
            for _ in range(4):
                at._dispatch_tool("t", {}, _call_counts=counts)
        self.assertEqual(counts, {})
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ), mock.patch(
            "services.agent.reviewer_exec._resolve_fn",
            return_value=lambda **k: {"ok": True},
        ):
            good = at._dispatch_tool("t", {}, _call_counts=counts)
        self.assertIs(good["ok"], True)
        self.assertEqual(counts, {"t": 1})

    def test_disabled_tool_teaches_instead_of_raising(self):
        """A Settings kill-switch is a user choice, not a crash."""
        from services.agent import agent_turn as at

        cfg_opts = {
            "enabled": False,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {},
            "prompt_note": "",
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ):
            res = at._dispatch_tool("t", {}, _call_counts={})
        self.assertIs(res["ok"], False)
        self.assertEqual(res["status"], "tool_disabled")
        self.assertIn("Settings", res["error"])
        self.assertTrue(at._is_policy_refusal(res))

    def test_disabled_tool_is_never_advertised(self):
        """Dispatch can only refuse it, so the schema must not offer it."""
        from services.agent import tool_schemas as ts

        def dummy(path: str):
            """Read a file."""
            return {}

        cfg_opts = {
            "enabled": False,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {},
            "prompt_note": "",
            "description_override": "",
            "schema_trim": None,
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ):
            text, meta = ts.render_tools_block(
                ["dummy"], resolver=lambda n: dummy,
                include_category_menu=False)
        self.assertEqual(text, "")
        self.assertEqual(meta["disabled"], ["dummy"])
        self.assertEqual(meta["rendered"], [])

    def test_effective_cap_is_advertised_in_the_tools_slot(self):
        """An undisclosed cap cannot be planned around."""
        from services.agent import tool_schemas as ts

        def dummy(query: str):
            """FIND sources."""
            return {}

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": 3,
            "param_defaults": {},
            "prompt_note": "",
            "description_override": "",
            "schema_trim": None,
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ):
            text, _meta = ts.render_tools_block(
                ["dummy"], resolver=lambda n: dummy,
                include_category_menu=False)
        self.assertIn("Per-turn limit: at most 3", text)

    def test_error_hint_appended(self):
        from services.agent import agent_turn as at

        def boom(**kwargs):
            raise ValueError("bad args")

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {},
            "prompt_note": "",
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ), mock.patch(
            "services.agent.reviewer_exec._resolve_fn", return_value=boom
        ):
            with self.assertRaises(ValueError) as cm:
                at._dispatch_tool("cam_generate", {})
        self.assertIn("Hint:", str(cm.exception))


class TestTimedToolRecovery(unittest.TestCase):
    """Exercise actual blocked workers through the online CAD dispatch boundary."""

    def setUp(self):
        self.options = {
            "enabled": True, "timeout_s": 0.02, "max_calls_per_turn": 100,
            "param_defaults": {}, "prompt_note": "",
        }
        patch = mock.patch(
            "services.agent.config.get_tool_options", return_value=self.options,
        )
        patch.start()
        self.addCleanup(patch.stop)

    def worker(self, *, failure=None, raised=None, inspect_context=None):
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        executions = []
        threads = []

        def work(value=1, doc_id=None, actor=None, run_id=None):
            with lock:
                executions.append((value, doc_id, actor, run_id))
                number = len(executions)
                threads.append(threading.current_thread())
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test did not release the real tool worker")
            if raised is not None:
                raise raised
            result = {"ok": failure is None, "execution": number, "computed": value * value}
            if failure is not None:
                result["error"] = failure
            if inspect_context is not None:
                result.update(inspect_context())
            return result

        def cleanup():
            release.set()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "tool worker did not exit")

        self.addCleanup(cleanup)
        return work, entered, release, executions

    def binding(self, handler, context=None, **kwargs):
        from services.agent.chudcode_tools import HostToolBinding

        if context is None:
            context = {
                "run_id": "run-a", "session_id": "session-a",
                "doc_id": "doc-a", "actor": "actor-a", "tool_call_counts": {},
            }
        return HostToolBinding(
            name="timed_recovery_test", description="Compute a value.", input_schema={},
            handler=handler, context=context, **kwargs,
        )

    def call(self, binding, args=None, *, agent_id=None, cancel=None):
        from services.agent.chudcode_tools import dispatch_host_tool

        return dispatch_host_tool(
            binding, args or {}, None, cancel, agent_id=agent_id,
        )

    def test_timeout_retry_recovers_original_then_same_input_runs_fresh(self):
        work, entered, release, executions = self.worker()
        binding = self.binding(work)
        original = self.call(binding, {"value": 7})
        self.assertTrue(entered.wait(1))
        self.assertEqual(original["status"], "still_running")
        self.assertTrue(original["timed_out"])
        self.assertTrue(original["in_flight"])
        self.assertTrue(original["started_new"])
        self.assertEqual(original["timeout_s"], 0.02)
        self.assertIn("ORIGINAL", original["error"])

        retry = self.call(binding, {"value": 7})
        self.assertEqual(retry["status"], "still_running")
        self.assertFalse(retry["started_new"])
        self.assertEqual(len(executions), 1)
        self.assertEqual(binding.context["tool_call_counts"], {"timed_recovery_test": 1})

        release.set()
        self.options["timeout_s"] = 1
        recovered = self.call(binding, {"value": 7})
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["execution"], 1)
        self.assertEqual(recovered["computed"], 49)
        fresh = self.call(binding, {"value": 7})
        self.assertEqual(fresh["execution"], 2)
        self.assertEqual(len(executions), 2)
        self.assertEqual(binding.context["tool_call_counts"], {"timed_recovery_test": 2})

    def test_creating_call_retries_after_current_document_changes_without_duplicate(self):
        from services.mcp.cad_common import _CUR, agent_doc_context

        work, entered, release, executions = self.worker()
        created = []

        # Like new_part(fresh=True), this signature creates rather than targets a
        # document, and its save can block after making that new document current.
        def create(name="part", *, fresh=False):
            document = f"created-{len(created) + 1}"
            created.append(document)
            _CUR["doc_id"] = document
            result = work()
            return {**result, "doc_id": document, "name": name}

        binding = self.binding(create)
        with agent_doc_context(None, scope="run-a"):
            previous = _CUR["doc_id"]
            _CUR["doc_id"] = "before-create"
            try:
                original = self.call(binding, {"name": "fixture", "fresh": True})
                self.assertTrue(entered.wait(1))
                self.assertEqual(_CUR["doc_id"], "created-1")
                self.assertTrue(original["started_new"])
                retry = self.call(binding, {"name": "fixture", "fresh": True})
                self.assertEqual(retry["status"], "still_running")
                self.assertFalse(retry["started_new"])
                self.assertEqual(created, ["created-1"])
                release.set()
                self.options["timeout_s"] = 1
                result = self.call(binding, {"name": "fixture", "fresh": True})
                self.assertEqual(result["doc_id"], "created-1")
                self.assertEqual(result["execution"], 1)
                self.assertEqual(created, ["created-1"])
                self.assertEqual(len(executions), 1)
            finally:
                _CUR["doc_id"] = previous

    def test_implicit_document_selector_keeps_different_current_targets_separate(self):
        from services.mcp.cad_common import _CUR, agent_doc_context, current_doc_id
        from services.mcp.tool_registry import model_selectors

        work, entered, release, executions = self.worker()

        @model_selectors("doc_id")
        def inspect_document(doc_id=None):
            return work(doc_id=doc_id or current_doc_id())

        binding = self.binding(inspect_document)
        with agent_doc_context(None, scope="run-a"):
            previous = _CUR["doc_id"]
            try:
                for document in ("implicit-a", "implicit-b"):
                    _CUR["doc_id"] = document
                    entered.clear()
                    self.assertTrue(self.call(binding)["started_new"])
                    self.assertTrue(entered.wait(1))
                _CUR["doc_id"] = "implicit-a"
                self.assertFalse(self.call(binding)["started_new"])
                self.assertEqual([item[1] for item in executions], ["implicit-a", "implicit-b"])
                release.set()
                self.options["timeout_s"] = 1
                self.assertEqual(self.call(binding)["execution"], 1)
                _CUR["doc_id"] = "implicit-b"
                self.assertEqual(self.call(binding)["execution"], 2)
                self.assertEqual(len(executions), 2)
            finally:
                _CUR["doc_id"] = previous

    def test_two_identical_waiters_both_receive_same_terminal_completion(self):
        from services.agent.agent_turn import _ToolCallRecovery

        work, entered, release, executions = self.worker()
        binding = self.binding(work)
        recovery = _ToolCallRecovery()
        binding.context["tool_call_recovery"] = recovery
        self.options["timeout_s"] = 5
        joined = threading.Barrier(3)
        original_wait = recovery.wait
        results, errors = [], []

        def wait_together(*args, **kwargs):
            joined.wait(timeout=5)
            return original_wait(*args, **kwargs)

        def invoke():
            try:
                results.append(self.call(binding, {"value": 4}))
            except BaseException as exc:
                errors.append(exc)

        callers = [threading.Thread(target=invoke) for _ in range(2)]
        with mock.patch.object(recovery, "wait", side_effect=wait_together):
            try:
                for caller in callers:
                    caller.start()
                joined.wait(timeout=5)
                self.assertTrue(entered.wait(1))
                self.assertEqual(len(executions), 1)
                release.set()
                for caller in callers:
                    caller.join(timeout=5)
                    self.assertFalse(caller.is_alive())
            finally:
                release.set()
                joined.abort()
                for caller in callers:
                    caller.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["execution"], 1)
        self.assertEqual(results[0]["computed"], 16)
        self.assertEqual(len(executions), 1)
        self.assertEqual(binding.context["tool_call_counts"], {"timed_recovery_test": 1})

    def test_overlapping_identical_call_does_not_start_second_worker(self):
        work, entered, release, executions = self.worker()
        binding = self.binding(work)
        self.options["timeout_s"] = 10
        original = []
        errors = []

        def invoke():
            try:
                original.append(self.call(binding, {"value": 3}))
            except BaseException as exc:
                errors.append(exc)

        caller = threading.Thread(target=invoke)
        caller.start()
        try:
            self.assertTrue(entered.wait(1))
            self.options["timeout_s"] = 0.02
            retry = self.call(binding, {"value": 3})
            self.assertEqual(original, [])
            self.assertFalse(retry["started_new"])
            self.assertEqual(len(executions), 1)
            release.set()
            caller.join(timeout=1)
            self.assertFalse(caller.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(original[0]["execution"], 1)
            self.options["timeout_s"] = 1
            self.assertEqual(self.call(binding, {"value": 3})["execution"], 2)
        finally:
            release.set()
            caller.join(timeout=5)

    def test_recovery_bypasses_execution_cap_but_new_call_does_not(self):
        work, entered, release, executions = self.worker()
        binding = self.binding(work)
        self.options["max_calls_per_turn"] = 1
        self.assertEqual(self.call(binding)["status"], "still_running")
        self.assertTrue(entered.wait(1))
        retry = self.call(binding)
        self.assertEqual(retry["status"], "still_running")
        self.assertFalse(retry["started_new"])
        release.set()
        self.options["timeout_s"] = 1
        self.assertEqual(self.call(binding)["execution"], 1)
        self.assertEqual(self.call(binding)["status"], "call_cap_reached")
        self.assertEqual(len(executions), 1)

    def test_terminal_failure_result_is_retained_without_duplicate_execution(self):
        work, entered, release, executions = self.worker(failure="solver refused the model")
        binding = self.binding(work)
        self.assertEqual(self.call(binding)["status"], "still_running")
        self.assertTrue(entered.wait(1))
        release.set()
        self.options["timeout_s"] = 1
        failed = self.call(binding)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "solver refused the model")
        self.assertEqual(failed["execution"], 1)
        self.assertEqual(len(executions), 1)
        self.assertEqual(self.call(binding)["execution"], 2)

    def test_terminal_exception_preserves_original_type_instance_and_detail(self):
        class NativeFailure(RuntimeError):
            pass

        failure = NativeFailure("native computation failed")
        failure.code = "solver_failed"
        failure.detail = {"converged": False}
        work, entered, release, executions = self.worker(raised=failure)
        binding = self.binding(work)
        self.assertEqual(self.call(binding)["status"], "still_running")
        self.assertTrue(entered.wait(1))
        release.set()
        self.options["timeout_s"] = 1
        with self.assertRaises(NativeFailure) as caught:
            self.call(binding)
        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.code, "solver_failed")
        self.assertEqual(caught.exception.detail, {"converged": False})
        self.assertEqual(len(executions), 1)
        with self.assertRaises(NativeFailure):
            self.call(binding)
        self.assertEqual(len(executions), 2)

    def test_tool_raised_timeout_error_is_terminal_before_and_after_pending_wait(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                failure = TimeoutError("native operation reached its own deadline")
                work, entered, release, executions = self.worker(raised=failure)
                binding = self.binding(work)
                self.options["timeout_s"] = 0.02
                if pending:
                    self.assertEqual(self.call(binding)["status"], "still_running")
                    self.assertTrue(entered.wait(1))
                release.set()
                self.options["timeout_s"] = 5
                with self.assertRaises(TimeoutError) as caught:
                    self.call(binding)
                self.assertIs(caught.exception, failure)
                self.assertEqual(len(executions), 1)
                self.assertEqual(
                    binding.context["tool_call_counts"], {"timed_recovery_test": 1},
                )
                self.assertEqual(binding.context["tool_call_recovery"].pending, {})

    def test_late_validation_refusal_refunds_cap_once_across_terminal_waiters(self):
        from services.agent.agent_turn import _reconcile_kwargs

        validating = threading.Event()
        release_validation = threading.Event()
        release_waiter = threading.Event()
        terminal_observed = threading.Event()
        validation_threads, body_calls = [], []
        bad_args = {}

        def work(value):
            body_calls.append(value)
            return {"ok": True, "computed": value * value}

        def reconcile(*args, **kwargs):
            if "value" not in args[2]:
                validation_threads.append(threading.current_thread())
                validating.set()
                if not release_validation.wait(5):
                    raise RuntimeError("test did not release argument validation")
            return _reconcile_kwargs(*args, **kwargs)

        def cleanup():
            release_validation.set()
            release_waiter.set()
            for thread in validation_threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        binding = self.binding(work)
        self.options["max_calls_per_turn"] = 1
        with mock.patch("services.agent.agent_turn._reconcile_kwargs", side_effect=reconcile):
            self.assertEqual(self.call(binding, bad_args)["status"], "still_running")
            self.assertTrue(validating.wait(1))
            self.assertFalse(self.call(binding, bad_args)["started_new"])
            self.assertEqual(body_calls, [])
            self.assertEqual(binding.context["tool_call_counts"], {"timed_recovery_test": 1})
            recovery = binding.context["tool_call_recovery"]
            original_wait = recovery.wait
            joined = threading.Barrier(2)
            results, errors = [], []
            self.options["timeout_s"] = 5

            def wait_together(*args, **kwargs):
                joined.wait(timeout=5)
                release_validation.set()
                result = original_wait(*args, **kwargs)
                if threading.current_thread() is delayed:
                    terminal_observed.set()
                    if not release_waiter.wait(5):
                        raise RuntimeError("test did not release terminal observer")
                return result

            def invoke():
                try:
                    results.append(self.call(binding, bad_args))
                except BaseException as exc:
                    errors.append(exc)

            delayed = threading.Thread(target=invoke)
            try:
                with mock.patch.object(recovery, "wait", side_effect=wait_together):
                    delayed.start()
                    refused = self.call(binding, bad_args)
                self.assertTrue(terminal_observed.wait(1))
                self.assertEqual(refused["status"], "missing_argument")
                self.assertEqual(binding.context["tool_call_counts"], {})
                self.assertEqual(body_calls, [])
                self.assertEqual(self.call(binding, {"value": 6})["computed"], 36)
                self.assertEqual(binding.context["tool_call_counts"], {"timed_recovery_test": 1})
                # The other observer returns only after a corrected invocation has
                # spent the refunded slot. It must not refund that new execution.
                release_waiter.set()
                delayed.join(timeout=5)
                self.assertFalse(delayed.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results, [refused])
                self.assertEqual(binding.context["tool_call_counts"], {"timed_recovery_test": 1})
                self.assertEqual(self.call(binding, {"value": 7})["status"], "call_cap_reached")
                self.assertEqual(body_calls, [6])
                self.assertEqual(len(validation_threads), 1)
            finally:
                joined.abort()
                release_validation.set()
                release_waiter.set()
                delayed.join(timeout=5)

    def test_arguments_document_actor_agent_run_session_and_roots_are_isolated(self):
        from dataclasses import replace
        from services.agent.agent_turn import _ToolCallRecovery

        work, entered, release, executions = self.worker()
        base = self.binding(work)
        base.context["tool_call_recovery"] = _ToolCallRecovery()
        cases = [
            (base, {"value": 2, "doc_id": "doc-a"}, None),
            (base, {"value": 3, "doc_id": "doc-a"}, None),
            (base, {"value": 2, "doc_id": "doc-b"}, None),
            (base, {"value": 2, "doc_id": "doc-a"}, "child"),
            (replace(base, context={**base.context, "actor": "actor-b"}),
             {"value": 2, "doc_id": "doc-a"}, None),
            (replace(base, context={**base.context, "run_id": "run-b"}),
             {"value": 2, "doc_id": "doc-a"}, None),
            (replace(base, context={**base.context, "session_id": "session-b"}),
             {"value": 2, "doc_id": "doc-a"}, None),
            (replace(base, trusted_context={"actor_key": "another-trusted-actor"}),
             {"value": 2, "doc_id": "doc-a"}, None),
            (replace(base, project_root="/different-project"),
             {"value": 2, "doc_id": "doc-a"}, None),
        ]
        for binding, args, agent in cases:
            entered.clear()
            result = self.call(binding, args, agent_id=agent)
            self.assertTrue(entered.wait(1))
            self.assertEqual(result["status"], "still_running")
            self.assertTrue(result["started_new"])
        self.assertTrue(entered.wait(1))
        self.assertEqual(len(executions), len(cases))
        # JSON object key order does not turn an exact retry into another job.
        retry = self.call(base, {"doc_id": "doc-a", "value": 2})
        self.assertFalse(retry["started_new"])
        self.assertEqual(len(executions), len(cases))
        release.set()
        self.options["timeout_s"] = 1
        recovered = [
            self.call(binding, args, agent_id=agent)["execution"]
            for binding, args, agent in cases
        ]
        self.assertEqual(len(set(recovered)), len(cases))
        self.assertEqual(len(executions), len(cases))

    def test_cancelled_wait_retains_original_native_outcome(self):
        work, entered, release, executions = self.worker()
        binding = self.binding(work)
        cancel = threading.Event()
        self.options["timeout_s"] = 10
        results = []
        caller = threading.Thread(
            target=lambda: results.append(self.call(binding, cancel=cancel)),
        )
        caller.start()
        try:
            self.assertTrue(entered.wait(1))
            cancel.set()
            caller.join(timeout=1)
            self.assertFalse(caller.is_alive())
            self.assertEqual(results[0]["status"], "cancelled")
            self.assertTrue(results[0]["in_flight"])
            self.assertFalse(results[0]["timed_out"])
            self.assertEqual(len(executions), 1)
            release.set()
            self.options["timeout_s"] = 1
            self.assertEqual(self.call(binding)["execution"], 1)
        finally:
            release.set()
            caller.join(timeout=5)

    def test_worker_keeps_original_authority_roots_and_helper_after_timeout(self):
        from services.agent import request_context
        from services.agent.online_turn import current_helper_runtime

        helper = object()

        def inspect_context():
            return {
                "authority": request_context.current_context().get("actor_key"),
                "root": request_context.root_value("CHUDWARE_PROJECT_DIR"),
                "original_helper": current_helper_runtime() is helper,
            }

        work, entered, release, executions = self.worker(inspect_context=inspect_context)
        binding = self.binding(
            work, trusted_context={"actor_key": "original-actor"},
            project_root="/original-project",
        )
        binding.context["helper_runtime"] = helper
        self.assertEqual(self.call(binding)["status"], "still_running")
        self.assertTrue(entered.wait(1))
        binding.context.pop("helper_runtime")
        release.set()
        self.options["timeout_s"] = 1
        result = self.call(binding)
        self.assertEqual(result["authority"], "original-actor")
        self.assertEqual(result["root"], "/original-project")
        self.assertTrue(result["original_helper"])
        self.assertEqual(len(executions), 1)


class TestPromptNote(unittest.TestCase):
    def test_render_includes_note(self):
        from services.agent import tool_schemas as ts

        def dummy(x: int = 1):
            """Does a thing."""
            return x

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {},
            "prompt_note": "Sanity-check fixtures first.",
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ):
            text, meta = ts.render_tools_block(
                ["dummy"],
                resolver=lambda n: dummy if n == "dummy" else None,
                budget_tokens=2000,
                include_category_menu=False,
            )
        self.assertIn("Note: Sanity-check fixtures first.", text)

    def test_render_includes_effective_param_defaults(self):
        from services.agent import tool_schemas as ts

        def dummy(path: str, limit: int = 0):
            """Read rows from a CSV."""
            return {"path": path, "limit": limit}

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {"limit": 500},
            "prompt_note": "",
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ):
            text, _ = ts.render_tools_block(
                ["office_read_csv"],
                resolver=lambda name: dummy if name == "office_read_csv" else None,
                budget_tokens=2000,
                include_category_menu=False,
            )

        self.assertIn("Effective defaults when omitted: limit=500.", text)

    def test_description_override_in_tools_block(self):
        from services.agent import tool_schemas as ts

        def dummy(x: int = 1) -> dict:
            """Original docstring that should be replaced."""
            return {"ok": True, "x": x}

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": None,
            "param_defaults": {},
            "prompt_note": "",
            "description_override": "Custom included tool context.",
            "schema_trim": True,
        }
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ):
            text, _meta = ts.render_tools_block(
                ["dummy"],
                resolver=lambda n: dummy if n == "dummy" else None,
                budget_tokens=2000,
                include_category_menu=False,
            )
        self.assertIn("Custom included tool context.", text)
        self.assertNotIn("Original docstring", text)


class TestDesignRecipe(unittest.TestCase):
    def test_catalog_and_match(self):
        from services.agent.design_recipe import design_recipe

        cat = design_recipe("")
        self.assertTrue(cat.get("ok"))
        self.assertGreaterEqual(len(cat.get("recipes") or []), 12)
        hit = design_recipe("M6 bolt circle")
        ids = [r["id"] for r in hit.get("recipes") or []]
        self.assertIn("bolt_circle_holes", ids)
        top = hit["recipes"][0]
        self.assertTrue(top.get("steps"))

    def test_recipe_metadata_and_live_tool_spellings(self):
        from services.agent.design_recipe import design_recipe

        pcb = next(r for r in design_recipe("PCB from schematic", 20)["recipes"]
                   if r["id"] == "pcb_from_schematic")
        self.assertEqual(pcb["schema"], "aicad.recipe/1")
        self.assertIn("ecad", pcb["domains"])
        tools = [step["tool"] for step in pcb["steps"]]
        self.assertIn("ecad_place_symbol", tools)
        self.assertIn("ecad_run_drc", tools)
        self.assertFalse({"place_symbol", "wire", "run_erc", "autoroute", "run_drc"} & set(tools))

        sheet = next(r for r in design_recipe("sheet metal box", 20)["recipes"]
                     if r["id"] == "sheet_metal_box")
        self.assertNotIn("bend_allowance", [step["tool"] for step in sheet["steps"]])

    def test_malformed_recipe_is_rejected(self):
        from services.agent.design_recipe import _validate_recipe

        self.assertIsNone(_validate_recipe(
            {"schema": "aicad.recipe/1", "id": "bad", "title": "Bad",
             "summary": "Missing steps"}, fallback_id="bad"))
        self.assertIsNone(_validate_recipe(
            {"schema": "wrong/1", "id": "bad", "title": "Bad",
             "summary": "Wrong schema", "steps": [{"tool": "x"}]},
            fallback_id="bad"))


class TestHints(unittest.TestCase):
    def test_hint_for_known(self):
        from services.agent.tool_hints import hint_for

        self.assertIsNotNone(hint_for("cam_generate"))
        self.assertIsNone(hint_for("no_such_tool_zzz"))


if __name__ == "__main__":
    unittest.main()
