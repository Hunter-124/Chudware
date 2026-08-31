"""Regression tests for the single Chudcode compaction authority."""

from __future__ import annotations

from services.agent import compaction


def _messages(count: int = 12) -> list[dict]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"message {index} " + ("geometry detail " * 40),
        }
        for index in range(count)
    ]


def test_local_archive_is_text_only() -> None:
    result = compaction.snapcompact_archive(
        _messages(), keep_recent_tokens=20, supersede_reads=False
    )

    assert result["archive"]["text"]
    assert "snap" not in result["archive"]
    assert "frames" not in result["archive"]
    assert "warning" not in result["archive"]


def test_reinject_legacy_snap_metadata_is_text_only() -> None:
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
