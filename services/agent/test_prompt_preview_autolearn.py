"""Tests for Settings → AI preview + Auto-Learn config/sidecar."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class TestAutolearnConfig(unittest.TestCase):
    def test_defaults(self):
        from services.agent import config as cfg

        self.assertFalse(cfg.autolearn_enabled({}))
        self.assertFalse(cfg.autolearn_auto_continue({}))
        self.assertEqual(cfg.autolearn_min_tool_calls({}), 5)
        self.assertEqual(cfg.memory_model({}), "online")

    def test_reads_agent_block(self):
        from services.agent import config as cfg

        c = {
            "agent": {
                "autolearn_enabled": True,
                "autolearn_auto_continue": True,
                "autolearn_min_tool_calls": 3,
                "memory_model": "local",
            }
        }
        self.assertTrue(cfg.autolearn_enabled(c))
        self.assertTrue(cfg.autolearn_auto_continue(c))
        self.assertEqual(cfg.autolearn_min_tool_calls(c), 3)
        self.assertEqual(cfg.memory_model(c), "local")

    def test_memory_model_invalid_falls_back(self):
        from services.agent import config as cfg

        self.assertEqual(cfg.memory_model({"agent": {"memory_model": "nope"}}), "online")


class TestStandingGuidance(unittest.TestCase):
    def test_empty_when_disabled(self):
        from services.agent.autolearn import standing_guidance

        self.assertEqual(standing_guidance({"agent": {"autolearn_enabled": False}}), "")

    def test_present_when_enabled(self):
        from services.agent.autolearn import standing_guidance

        g = standing_guidance({"agent": {"autolearn_enabled": True}})
        self.assertIn("Auto-Learn", g)
        self.assertIn("ledger_note", g)


class TestMaybeCapture(unittest.TestCase):
    def test_skips_when_disabled(self):
        from services.agent.autolearn import maybe_capture_after_turn

        out = maybe_capture_after_turn(
            request="x", result=None, tool_calls=20,
            cfg={"agent": {"autolearn_enabled": False}},
        )
        self.assertTrue(out["skipped"])

    def test_skips_below_min_calls(self):
        from services.agent.autolearn import maybe_capture_after_turn

        out = maybe_capture_after_turn(
            request="x", result=None, tool_calls=1,
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
                    "memory_backend": "sqlite",
                },
            }
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

    def test_main_chat_preview_assembles(self):
        from services.agent.prompt_preview import (
            USER_REQUEST_PLACEHOLDER,
            preview_main_chat_system,
        )

        out = preview_main_chat_system(include_tools=False)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out.get("system"), "expected non-empty system")
        self.assertEqual(out.get("user_placeholder"), USER_REQUEST_PLACEHOLDER)
        self.assertIn("Chudware", out["system"])
        self.assertGreater(out["meta"].get("system_chars", 0), 100)


    def test_tool_catalog_covers_registry_and_guidance(self):
        from services.agent.prompt_preview import tool_catalog

        out = tool_catalog()
        self.assertTrue(out.get("ok"), out.get("error"))
        rows = out.get("tools") or []
        self.assertGreater(len(rows), 300)
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
