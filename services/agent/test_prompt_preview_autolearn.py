"""Tests for Settings → AI preview + Auto-Learn config/sidecar."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path


class TestAutolearnConfig(unittest.TestCase):
    def test_defaults(self):
        from services.agent import config as cfg

        self.assertFalse(cfg.autolearn_enabled({}))
        self.assertFalse(cfg.autolearn_auto_continue({}))

    def test_reads_agent_block(self):
        from services.agent import config as cfg

        c = {
            "agent": {
                "autolearn_enabled": True,
                "autolearn_auto_continue": True,
                "autolearn_min_tool_calls": 3,
            }
        }
        self.assertTrue(cfg.autolearn_enabled(c))
        self.assertTrue(cfg.autolearn_auto_continue(c))
        self.assertEqual(cfg.autolearn_min_tool_calls(c), 3)


class TestStandingGuidance(unittest.TestCase):
    def test_empty_when_disabled(self):
        from services.agent.autolearn import standing_guidance

        self.assertEqual(standing_guidance({"agent": {"autolearn_enabled": False}}), "")



class TestMaybeCapture(unittest.TestCase):
    def test_skips_when_disabled(self):
        from services.agent.autolearn import maybe_capture_after_turn

        out = maybe_capture_after_turn(
            request="x",
            result=None,
            tool_calls=20,
            cfg={"agent": {"autolearn_enabled": False}},
        )
        self.assertTrue(out["skipped"])

    def test_skips_below_min_calls(self):
        from services.agent.autolearn import maybe_capture_after_turn

        out = maybe_capture_after_turn(
            request="x",
            result=None,
            tool_calls=1,
            cfg={
                "memory_enabled": True,
                "agent": {
                    "autolearn_enabled": True,
                    "autolearn_auto_continue": True,
                    "autolearn_min_tool_calls": 5,
                    "memory_learn": True,
                },
            },
        )
        self.assertTrue(out["skipped"])
        self.assertIn("below_min", out["reason"])

    def test_writes_memory_when_enabled(self):
        from services.agent.autolearn import maybe_capture_after_turn
        from services.agent import project_memory as pm

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".plm").mkdir()

            class _R:
                stop_reason = "done"
                result = {"handoff": {"summary": "built a flange"}, "tool_calls": 6}

            cfg = {
                "memory_enabled": True,
                "agent": {
                    "autolearn_enabled": True,
                    "autolearn_auto_continue": True,
                    "autolearn_min_tool_calls": 5,
                    "memory_learn": True,
                },
            }
            with mock.patch.object(pm, "_offline_memory", return_value=True):
                out = maybe_capture_after_turn(
                    request="make a flange",
                    result=_R(),
                    tool_calls=6,
                    project_root=str(root),
                    cfg=cfg,
                )
                self.assertTrue(out.get("ok"), out)
                hits = pm.recall(root, "auto-learn flange", limit=5, token_budget=500)
            self.assertTrue(
                any("[auto-learn]" in (h.get("content") or "") for h in hits),
                hits,
            )


class TestPromptPreview(unittest.TestCase):
    def test_tool_entry_ok_or_unresolved(self):
        from services.agent.prompt_preview import preview_tool_entry

        out = preview_tool_entry("cad_guide")
        self.assertTrue(out.get("ok"), out)
        self.assertIn("cad_guide", out.get("text") or out.get("entry") or "")

    def test_tool_entry_empty_name(self):
        from services.agent.prompt_preview import preview_tool_entry

        out = preview_tool_entry("")
        self.assertFalse(out["ok"])


    def test_check_with_me_preview_preserves_custom_prompt_and_stable_tool_surface(self):
        from services.agent.prompt_preview import preview_main_chat_system

        with mock.patch(
            "services.agent.config.load_config",
            return_value={"system_prompt": "CUSTOM GLOBAL SYSTEM",
                          "copilot": {"work_style": "check_with_me"}},
        ):
            out = preview_main_chat_system(include_tools=True)
        with mock.patch(
            "services.agent.config.load_config",
            return_value={"system_prompt": "CUSTOM GLOBAL SYSTEM",
                          "copilot": {"work_style": "do_it"}},
        ):
            other = preview_main_chat_system(include_tools=True)

        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out["system"].startswith("CUSTOM GLOBAL SYSTEM"))
        self.assertTrue(other.get("ok"), other)
        self.assertEqual(out["host_tools"], other["host_tools"])
        self.assertIn("describe_part", {tool["name"] for tool in out["host_tools"]})

    def test_tool_entry_shows_effective_defaults(self):
        from services.agent.prompt_preview import preview_tool_entry

        with mock.patch(
            "services.agent.config.get_tool_options",
            return_value={
                "enabled": True,
                "timeout_s": None,
                "max_calls_per_turn": None,
                "param_defaults": {"limit": 500},
                "prompt_note": "",
                "description_override": "",
                "schema_trim": None,
            },
        ):
            out = preview_tool_entry("office_read_csv")
        self.assertTrue(out.get("ok"), out)
        self.assertIn("Effective defaults when omitted: limit=500.", out["entry"])

    def test_settings_matches_online_and_offline_description_dials(self):
        from services.agent import agent_turn, config, omp_tools, tool_schemas
        from services.agent.prompt_preview import preview_tool_entry
        from services.agent.test_omp_tools import _context, _entry

        def probe(limit=10):
            """Original summary. Original detail."""
            return {"limit": limit}

        entries = {"probe": _entry(probe, "Original summary. Original detail.")}
        opts = {
            "description_override": "Custom summary. Custom detail.",
            "schema_trim": True,
            "param_defaults": {"limit": 23},
            "max_calls_per_turn": 3,
            "timeout_s": 7,
            "prompt_note": "Use the active document.",
        }
        def options(_cfg, name):
            return opts if name == "probe" else {}

        with mock.patch.object(config, "get_tool_options", side_effect=options), \
             mock.patch.object(config, "tool_max_calls_default", return_value=0), \
             mock.patch.object(omp_tools, "_load_registry_entries", return_value=entries), \
             mock.patch.object(agent_turn, "resolver_for", return_value=lambda name: probe if name == "probe" else None):
            binding = next(b for b in omp_tools.build_host_tools(
                _context(entries, categories=["probe"])) if b.name == "probe")
            preview = preview_tool_entry("probe")
            offline, _ = tool_schemas.render_tools_block(
                ["probe"], resolver=lambda name: probe, budget_tokens=2000,
                include_category_menu=False,
            )
            opts["schema_trim"] = False
            full_binding = next(b for b in omp_tools.build_host_tools(
                _context(entries, categories=["probe"])) if b.name == "probe")
            full_preview = preview_tool_entry("probe")
            full_offline, _ = tool_schemas.render_tools_block(
                ["probe"], resolver=lambda name: probe, budget_tokens=2000,
                include_category_menu=False,
            )
        self.assertTrue(preview["ok"], preview)
        rendered = tool_schemas._description_block(binding.description)
        self.assertIn(rendered, preview["entry"])
        self.assertIn(rendered, offline)
        self.assertNotIn("Custom detail.", binding.description)
        self.assertNotIn("Original summary.", binding.description)
        self.assertIn("limit=23", binding.description)
        self.assertIn("3 call(s)", binding.description)
        self.assertIn("7 s", binding.description)
        full_rendered = tool_schemas._description_block(full_binding.description)
        self.assertIn("Custom detail.", full_binding.description)
        self.assertIn(full_rendered, full_preview["entry"])
        self.assertIn(full_rendered, full_offline)

    def test_global_defaults_and_automatic_skill_delivery(self):
        from services.agent import agent_turn, config, omp_tools, tool_schemas
        from services.agent.prompt_preview import preview_tool_entry
        from services.agent.test_omp_tools import _alpha, _context, _entry

        entries = {
            "get_part": _entry(_alpha, "Inspect a part."),
            "probe": _entry(_alpha, "Inspect a fixture."),
        }
        resolver = lambda name: entries[name].fn if name in entries else None
        with mock.patch.object(config, "get_tool_options", return_value={}), \
             mock.patch.object(config, "tool_timeout_s_default", return_value=19), \
             mock.patch.object(config, "tool_max_calls_default", return_value=4), \
             mock.patch.object(omp_tools, "_auto_skill_tools", return_value={
                 "get_part": ["fixture-skill"], "probe": ["fixture-skill"],
             }), \
             mock.patch.object(omp_tools, "_load_registry_entries", return_value=entries), \
             mock.patch.object(agent_turn, "resolver_for", return_value=resolver):
            bindings = {b.name: b for b in omp_tools.build_host_tools(_context(entries))}
            preview = preview_tool_entry("get_part")
            deferred_preview = preview_tool_entry("probe")
            offline, _ = tool_schemas.render_tools_block(
                ["get_part"], resolver=resolver, include_category_menu=False,
            )
        active, deferred = bindings["get_part"], bindings["probe"]
        self.assertTrue(active.active)
        self.assertFalse(deferred.active)
        self.assertNotIn("fixture-skill", active.description)
        self.assertIn("fixture-skill", deferred.description)
        self.assertIn("19 s", active.description)
        self.assertIn("4 call(s)", active.description)
        self.assertIn(tool_schemas._description_block(active.description), preview["entry"])
        self.assertIn(tool_schemas._description_block(deferred.description), deferred_preview["entry"])
        self.assertIn("19 s", offline)
        self.assertIn("4 call(s)", offline)

    def test_online_preview_and_baseline_count_only_active_bindings(self):
        from services.agent import mission_dispatch, omp_tools, prompt_cache, roles
        from services.agent.prompt_preview import preview_main_chat_system

        active = omp_tools.HostToolBinding("active", "Active tool", {"type": "object"})
        inactive = omp_tools.HostToolBinding(
            "inactive", "Deferred tool", {"type": "object"}, active=False,
        )
        with mock.patch.object(prompt_cache, "fingerprint", return_value="fixture"), \
             mock.patch.object(roles, "load_role", return_value=object()), \
             mock.patch.object(mission_dispatch, "role_prompt", return_value=("system", [])), \
             mock.patch.object(mission_dispatch, "MissionDispatch"), \
             mock.patch.object(omp_tools, "build_host_tools", return_value=[active, inactive]):
            composed = prompt_cache.compose_online()
        with mock.patch.object(prompt_cache, "compose_online", return_value=composed), \
             mock.patch.object(prompt_cache, "online_prompt", return_value=composed):
            preview = preview_main_chat_system()
            baseline = prompt_cache.baseline()
        self.assertEqual([row["name"] for row in composed.host_tools], ["active"])
        self.assertEqual(preview["meta"]["allowed_tool_count"], 1)
        self.assertEqual(preview["meta"]["host_tool_names"], ["active"])
        self.assertEqual(baseline["allowed_tool_count"], 1)
        self.assertEqual(baseline["tokens"], composed.total_tokens)


    def test_tool_catalog_covers_registry_and_guidance(self):
        from services.agent.prompt_preview import tool_catalog

        out = tool_catalog()
        self.assertTrue(out.get("ok"), out.get("error"))
        rows = out.get("tools") or []
        by_name = {r["name"]: r for r in rows}
        # Registry tool: family + live description + curated guidance present.
        lg = by_name.get("ledger_get")
        self.assertIsNotNone(lg)
        self.assertEqual(lg["family"], "ledger")
        self.assertIn("design ledger", lg["description"])
        self.assertTrue(lg["resolved"])
        self.assertTrue(lg["guidance"].get("prompt_note"))
        # Pseudo-tool with no registry callable still listed, with guidance.
        dlg = by_name.get("delegate")
        self.assertIsNotNone(dlg)
        self.assertFalse(dlg["resolved"])
        self.assertTrue(dlg["guidance"].get("prompt_note"))


class TestWorkerPreviewMethods(unittest.TestCase):
    def test_registered(self):
        from services.agent.worker_rpc import WORKER_METHODS

        self.assertIn("ai_tool_preview", WORKER_METHODS)
        self.assertIn("ai_prompt_preview", WORKER_METHODS)
        self.assertIn("ai_tool_catalog", WORKER_METHODS)


if __name__ == "__main__":
    unittest.main()
