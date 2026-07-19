"""Unit tests for project-scoped SQLite FTS memory."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.agent import project_memory as pm


class TestProjectMemory(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        pm.close_all()

    def tearDown(self) -> None:
        pm.close_all()
        self._tmp.cleanup()

    def test_db_auto_created_under_plm(self) -> None:
        mid = pm.remember(self.root, "M3 clearance on all standoffs", kind="decision")
        self.assertIsNotNone(mid)
        db = pm.memory_db_path(self.root)
        self.assertTrue(db.is_file())
        self.assertEqual(db.parent.name, ".plm")

    def test_recall_finds_content(self) -> None:
        pm.remember(self.root, "Use 6061-T6 aluminum for the chassis", kind="material")
        pm.remember(self.root, "Fasteners are M3 socket head", kind="fastener")
        hits = pm.recall(self.root, "aluminum chassis material", limit=5, token_budget=400)
        self.assertTrue(hits)
        blob = " ".join(h["content"] for h in hits).lower()
        self.assertIn("6061", blob)

    def test_dedupe_same_content(self) -> None:
        a = pm.remember(self.root, "Keep wall thickness >= 2mm")
        b = pm.remember(self.root, "Keep wall thickness >= 2mm")
        self.assertEqual(a, b)
        st = pm.stats(self.root)
        self.assertEqual(st["count"], 1)

    def test_token_budget_caps_payload(self) -> None:
        for i in range(10):
            pm.remember(
                self.root,
                f"Design note number {i}: " + ("bracket fillet radius detail. " * 20),
                kind="note",
            )
        hits = pm.recall(self.root, "bracket fillet", limit=10, token_budget=80)
        total = sum(int(h["tokens"]) for h in hits)
        self.assertLessEqual(total, 120)  # small slack for first-hit truncate
        self.assertGreaterEqual(len(hits), 1)

    def test_format_for_context_empty(self) -> None:
        self.assertEqual(pm.format_for_context([]), "")

    def test_format_for_context_nonempty(self) -> None:
        pm.remember(self.root, "Prefer blind rivets on skin panels")
        hits = pm.recall(self.root, "rivets skin", limit=3)
        block = pm.format_for_context(hits)
        self.assertIn("Project memory", block)
        self.assertIn("rivet", block.lower())


if __name__ == "__main__":
    unittest.main()
