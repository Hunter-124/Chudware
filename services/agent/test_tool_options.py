"""Tests for agent.tool_options + design_recipe + tool_hints."""
from __future__ import annotations

import unittest
from unittest import mock


class TestGetToolOptions(unittest.TestCase):
    def test_defaults_when_missing(self):
        from services.agent.config import get_tool_options

        opts = get_tool_options({}, "run_fea_static")
        self.assertTrue(opts["enabled"])
        self.assertIsNone(opts["timeout_s"])
        self.assertIsNone(opts["max_calls_per_turn"])
        self.assertEqual(opts["param_defaults"], {})
        self.assertEqual(opts["prompt_note"], "")

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
                    }
                }
            }
        }
        opts = get_tool_options(cfg, "research_web_search")
        self.assertEqual(opts["param_defaults"], {"max_results": 5})
        self.assertEqual(opts["prompt_note"], "Keep it short.")
        self.assertEqual(opts["timeout_s"], 30)
        self.assertEqual(opts["max_calls_per_turn"], 3)


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

    def test_max_calls_per_turn(self):
        from services.agent import agent_turn as at

        cfg_opts = {
            "enabled": True,
            "timeout_s": None,
            "max_calls_per_turn": 1,
            "param_defaults": {},
            "prompt_note": "",
        }
        counts: dict = {}
        with mock.patch(
            "services.agent.config.get_tool_options", return_value=cfg_opts
        ), mock.patch(
            "services.agent.reviewer_exec._resolve_fn",
            return_value=lambda **k: {"ok": True},
        ):
            at._dispatch_tool("t", {}, _call_counts=counts)
            with self.assertRaises(RuntimeError) as cm:
                at._dispatch_tool("t", {}, _call_counts=counts)
        self.assertIn("max_calls_per_turn", str(cm.exception))

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


class TestHints(unittest.TestCase):
    def test_hint_for_known(self):
        from services.agent.tool_hints import hint_for

        self.assertIsNotNone(hint_for("cam_generate"))
        self.assertIsNone(hint_for("no_such_tool_zzz"))


if __name__ == "__main__":
    unittest.main()
