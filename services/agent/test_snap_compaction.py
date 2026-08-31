"""Regression tests for the single Chudcode compaction authority."""

from __future__ import annotations

import sys
import types

from services.agent import compaction


class _PoisonModule(types.ModuleType):
    def __getattr__(self, name):
        raise AssertionError(
            f"ordinary compaction touched removed online snap path: {name}"
        )


def _messages(count: int = 12) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message {index} " + ("geometry detail " * 40),
        }
        for index in range(count)
    ]


def test_local_archive_never_calls_omp_or_snap_store(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "services.agent.omp_bridge", _PoisonModule("omp"))
    monkeypatch.setitem(
        sys.modules, "services.agent.snap_store", _PoisonModule("store")
    )

    result = compaction.snapcompact_archive(
        _messages(), keep_recent_tokens=20, supersede_reads=False
    )

    assert result["archive"]["text"]
    assert "snap" not in result["archive"]
    assert "frames" not in result["archive"]
    assert "warning" not in result["archive"]


def test_reinject_legacy_snap_metadata_is_text_only_and_does_not_resnap(
    monkeypatch,
) -> None:
    monkeypatch.setitem(sys.modules, "services.agent.omp_bridge", _PoisonModule("omp"))
    monkeypatch.setitem(
        sys.modules, "services.agent.snap_store", _PoisonModule("store")
    )
    legacy = {
        "summary": "Read HISTORY.",
        "archive": {
            "text": "old history",
            "text_head": "old history",
            "text_tail": "",
            "truncated_chars": 0,
            "snap": {
                "snap_id": "a" * 16,
                "api": "anthropic",
                "model": "old-model",
            },
        },
    }

    messages = compaction.reinject_archive(legacy)

    assert messages == [{"role": "user", "content": "Read HISTORY.\n\nold history"}]


def test_snap_store_remains_available_for_caller_sweep() -> None:
    from services.agent import snap_store

    assert callable(snap_store.put)
    assert callable(snap_store.get)
