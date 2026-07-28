"""Tests for agent.tool_options + design_recipe + tool_hints."""
from __future__ import annotations

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


class TestHints(unittest.TestCase):
    def test_hint_for_known(self):
        from services.agent.tool_hints import hint_for

        self.assertIsNotNone(hint_for("cam_generate"))
        self.assertIsNone(hint_for("no_such_tool_zzz"))


if __name__ == "__main__":
    unittest.main()
