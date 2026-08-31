"""Focused project-memory cutover tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.agent import project_memory as pm


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, dict] = {
            "memory_remember": {"ok": True, "id": "mem-17"},
            "memory_recall": {"ok": True, "hits": []},
            "memory_stats": {"ok": True, "count": 0, "exists": False},
        }
        self.error: Exception | None = None

    def memory_call(self, command: str, params: dict) -> dict:
        self.calls.append((command, dict(params)))
        if self.error is not None:
            raise self.error
        return dict(self.responses[command])


class TestProjectMemoryOffline(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._host = mock.patch.object(
            pm,
            "_default_host",
            side_effect=AssertionError("offline memory called Chudcode"),
        )
        self._host.start()
        self.root = Path(self._tmp.name)
        self._offline = mock.patch.object(pm, "_offline_memory", return_value=True)
        self._offline.start()
        pm.close_all()

    def tearDown(self) -> None:
        pm.close_all()
        self._offline.stop()
        self._host.stop()
        self._tmp.cleanup()

    def test_db_auto_created_under_project_plm(self) -> None:
        mid = pm.remember(
            self.root,
            "M3 clearance on all standoffs",
            kind="decision",
            source="drawing",
            importance=0.8,
        )
        self.assertIsNotNone(mid)
        db = pm.memory_db_path(self.root)
        self.assertTrue(db.is_file())
        self.assertEqual(db, self.root.resolve() / ".plm" / "memory.db")

    def test_offline_recall_is_project_scoped_and_bounded(self) -> None:
        other = self.root / "other"
        other.mkdir()
        pm.remember(self.root, "Use 6061 aluminum for the chassis", kind="material")
        pm.remember(other, "Use carbon fiber for the chassis", kind="material")
        for i in range(8):
            pm.remember(
                self.root,
                f"Design note {i}: " + ("bracket fillet radius detail. " * 20),
            )

        hits = pm.recall(self.root, "chassis aluminum", limit=5, token_budget=80)
        self.assertTrue(any("6061" in hit["content"] for hit in hits))
        self.assertFalse(any("carbon fiber" in hit["content"] for hit in hits))
        bounded = pm.recall(self.root, "bracket fillet", limit=20, token_budget=80)
        self.assertLessEqual(len(bounded), 20)
        self.assertLessEqual(sum(int(hit["tokens"]) for hit in bounded), 80)

    def test_dedupe_and_metadata_survive_offline(self) -> None:
        first = pm.remember(
            self.root,
            "Keep wall thickness >= 2mm",
            kind="constraint",
            source="review",
            importance=0.6,
        )
        second = pm.remember(
            self.root,
            "Keep wall thickness >= 2mm",
            kind="constraint",
            source="review",
            importance=0.9,
        )
        self.assertEqual(first, second)
        hits = pm.recall(self.root, "wall thickness", kind="constraint")
        self.assertEqual(hits[0]["kind"], "constraint")
        self.assertEqual(hits[0]["source"], "review")
        self.assertEqual(pm.stats(self.root)["count"], 1)


class TestProjectMemoryOnline(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.host = _FakeHost()
        self._online = mock.patch.object(pm, "_offline_memory", return_value=False)
        self._host = mock.patch.object(pm, "_default_host", return_value=self.host)
        self._online.start()
        self._host.start()
        pm.close_all()

    def tearDown(self) -> None:
        pm.close_all()
        self._host.stop()
        self._online.stop()
        self._tmp.cleanup()

    def test_online_remember_uses_process_global_host_and_preserves_fields(
        self,
    ) -> None:
        mid = pm.remember(
            self.root,
            "  Anodize   after tapping  ",
            kind="finish",
            source="reviewer",
            importance=2.0,
        )
        self.assertEqual(mid, pm._stable_int_id("mem-17"))
        self.assertEqual(len(self.host.calls), 1)
        command, payload = self.host.calls[0]
        self.assertEqual(command, "memory_remember")
        self.assertEqual(payload["root"], str(self.root.resolve()))
        self.assertEqual(payload["content"], "Anodize after tapping")
        self.assertEqual(payload["kind"], "finish")
        self.assertEqual(payload["source"], "reviewer")
        self.assertEqual(payload["importance"], 1.0)
        self.assertFalse(pm.memory_db_path(self.root).exists())

    def test_online_recall_forwards_scope_and_reapplies_bounds(self) -> None:
        self.host.responses["memory_recall"] = {
            "ok": True,
            "hits": [
                {
                    "id": f"m-{i}",
                    "content": f"note {i}: " + ("bracket fillet detail " * 30),
                    "kind": "decision",
                    "source": "agent",
                    "importance": 0.7,
                    "score": 1.0 - i / 100,
                    "tokens": 1,
                }
                for i in range(10)
            ],
        }
        hits = pm.recall(
            self.root,
            "bracket fillet",
            limit=7,
            token_budget=80,
            kind="decision",
        )
        command, payload = self.host.calls[0]
        self.assertEqual(command, "memory_recall")
        self.assertEqual(payload["root"], str(self.root.resolve()))
        self.assertEqual(payload["limit"], 7)
        self.assertEqual(payload["tokenBudget"], 80)
        self.assertEqual(payload["kind"], "decision")
        self.assertGreaterEqual(len(hits), 1)
        self.assertLessEqual(len(hits), 7)
        self.assertLessEqual(sum(int(hit["tokens"]) for hit in hits), 80)
        for hit in hits:
            self.assertEqual(hit["tokens"], pm.estimate_tokens(hit["content"]))

    def test_online_stats_keeps_plm_path_convention(self) -> None:
        self.host.responses["memory_stats"] = {
            "ok": True,
            "path": "/private/chudcode/store.db",
            "count": 3,
            "exists": True,
            "kinds": {"decision": 3},
        }
        result = pm.stats(self.root)
        self.assertEqual(result["path"], str(pm.memory_db_path(self.root)))
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["kinds"], {"decision": 3})

    def test_online_unavailable_is_honest_and_never_falls_back(self) -> None:
        self.host.error = RuntimeError("worker stopped")
        with self.assertRaisesRegex(pm.ProjectMemoryUnavailable, "worker stopped"):
            pm.remember(self.root, "Never silently redirect this memory")
        self.assertFalse(pm.memory_db_path(self.root).exists())

    def test_online_failure_result_is_honest(self) -> None:
        self.host.responses["memory_recall"] = {
            "ok": False,
            "reason": "no live Chudcode service",
        }
        with self.assertRaisesRegex(pm.ProjectMemoryUnavailable, "no live Chudcode"):
            pm.recall(self.root, "anything")


if __name__ == "__main__":
    unittest.main()
