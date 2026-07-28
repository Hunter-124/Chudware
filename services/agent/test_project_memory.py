"""Unit tests for project-scoped SQLite FTS memory."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.agent import project_memory as pm


class TestProjectMemory(unittest.TestCase):
    def setUp(self) -> None:
        # These tests assert SQLite-backend behavior (a .plm/memory.db file, the
        # FTS ranking, the local budget trim). The backend is user-configurable
        # (agent.memory_backend / CHUDWARE_MEMORY_BACKEND), and a developer who
        # switches Settings to "mnemopi" must not turn the suite red, so pin it
        # here instead of inheriting ambient config. The mnemopi path is covered
        # by TestProjectMemoryMnemopiBackend below.
        self._env = mock.patch.dict(os.environ, {"CHUDWARE_MEMORY_BACKEND": "sqlite"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        pm.close_all()

    def tearDown(self) -> None:
        pm.close_all()
        self._tmp.cleanup()

    def test_db_auto_created_under_plm(self) -> None:
        # SQLite-backend-only contract: mnemopi keeps memories in its own store
        # (vendor/omp/packages/mnemopi) and deliberately writes no local
        # .plm/memory.db, so this assertion is meaningful only for sqlite.
        self.assertEqual(pm._memory_backend(), "sqlite")
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


class _FakeBridgeUnavailable(RuntimeError):
    pass


class _FakeBridge:
    """Stand-in for services.agent.omp_bridge — no bun sidecar required."""

    BridgeUnavailable = _FakeBridgeUnavailable

    def __init__(self, hits: list[dict]) -> None:
        self._hits = hits
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "memory_recall":
            return {"hits": list(self._hits)}
        raise _FakeBridgeUnavailable(method)


class TestProjectMemoryMnemopiBackend(unittest.TestCase):
    """The token budget must hold on the bridge path too (real config uses it)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"CHUDWARE_MEMORY_BACKEND": "mnemopi"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _install(self, hits: list[dict]) -> _FakeBridge:
        bridge = _FakeBridge(hits)
        patch = mock.patch.object(pm, "_bridge", lambda: bridge)
        patch.start()
        self.addCleanup(patch.stop)
        return bridge

    def _long_hits(self, *, reported_tokens: int | None = None) -> list[dict]:
        hits = []
        for i in range(10):
            h = {
                "id": f"mem-{i}",
                "content": f"Design note number {i}: " + ("bracket fillet radius detail. " * 20),
                "kind": "note",
                "score": 1.0 - i / 100.0,
            }
            if reported_tokens is not None:
                h["tokens"] = reported_tokens
            hits.append(h)
        return hits

    def test_token_budget_caps_payload(self) -> None:
        # mnemopi ranks by relevance and is free to return far more text than the
        # tokenBudget hint, so the cap must be re-applied locally or project
        # memory overruns its context budget on every turn.
        bridge = self._install(self._long_hits())
        got = pm.recall(self.root, "bracket fillet", limit=10, token_budget=80)
        self.assertEqual([c[0] for c in bridge.calls], ["memory_recall"])
        self.assertEqual(bridge.calls[0][1]["tokenBudget"], 80)
        self.assertGreaterEqual(len(got), 1)
        total = sum(int(h["tokens"]) for h in got)
        self.assertLessEqual(total, 120)  # small slack for first-hit truncate

    def test_reported_tokens_describe_returned_text(self) -> None:
        # A backend that under-reports "tokens" must not smuggle text past the
        # cap: "tokens" always describes the text actually handed back.
        self._install(self._long_hits(reported_tokens=1))
        got = pm.recall(self.root, "bracket fillet", limit=10, token_budget=80)
        self.assertGreaterEqual(len(got), 1)
        for h in got:
            self.assertEqual(int(h["tokens"]), pm.estimate_tokens(h["content"]))
        self.assertLessEqual(sum(int(h["tokens"]) for h in got), 120)

    def test_short_hits_pass_through_untruncated(self) -> None:
        hits = [
            {"id": "a", "content": "Use 6061-T6 for the chassis", "kind": "material"},
            {"id": "b", "content": "Fasteners are M3 socket head", "kind": "fastener"},
        ]
        self._install(hits)
        got = pm.recall(self.root, "chassis fasteners", limit=5, token_budget=400)
        self.assertEqual([h["content"] for h in got], [h["content"] for h in hits])

    def test_bridge_unavailable_falls_back_to_sqlite(self) -> None:
        bridge = _FakeBridge([])

        def boom(method: str, params: dict) -> dict:
            bridge.calls.append((method, params))
            raise _FakeBridgeUnavailable(method)

        bridge.call = boom  # type: ignore[method-assign]
        patch = mock.patch.object(pm, "_bridge", lambda: bridge)
        patch.start()
        self.addCleanup(patch.stop)
        pm.close_all()
        self.addCleanup(pm.close_all)
        self.assertIsNotNone(pm.remember(self.root, "Anodize after tapping", kind="finish"))
        self.assertTrue(pm.memory_db_path(self.root).is_file())
        got = pm.recall(self.root, "anodize tapping", limit=5, token_budget=400)
        self.assertTrue(got)



if __name__ == "__main__":
    unittest.main()
