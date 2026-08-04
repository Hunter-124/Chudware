"""Direct tests for the shared lossless append-only JSONL writer."""
from __future__ import annotations

import importlib

from services.agent import raw_log


_HEADER = {"kind": "header", "schema": "test.raw/1", "owner": "conv_1"}


def test_header_is_written_once(tmp_path):
    path = tmp_path / "archive.jsonl"

    raw_log.append(path, {"kind": "message", "value": "first"}, _HEADER)
    raw_log.append(
        path,
        {"kind": "message", "value": "second"},
        {"kind": "header", "schema": "wrong/2"},
    )

    records = raw_log.read(path)
    assert records[0] == _HEADER
    assert [record["kind"] for record in records].count("header") == 1


def test_sequence_is_monotonic(tmp_path):
    path = tmp_path / "sequence.jsonl"

    for value in range(4):
        raw_log.append(path, {"kind": "message", "value": value}, _HEADER)

    assert [record["seq"] for record in raw_log.read(path)[1:]] == [1, 2, 3, 4]


def test_sequence_survives_a_simulated_restart(tmp_path):
    path = tmp_path / "restart.jsonl"
    raw_log.append(path, {"kind": "message", "value": 1}, _HEADER)
    raw_log.append(path, {"kind": "message", "value": 2}, _HEADER)

    restarted = importlib.reload(raw_log)
    restarted.append(path, {"kind": "message", "value": 3}, _HEADER)

    assert [record["seq"] for record in restarted.read(path)[1:]] == [1, 2, 3]

    headerless = tmp_path / "restart-headerless.jsonl"
    restarted.append(headerless, {"kind": "message", "value": 1})
    restarted = importlib.reload(restarted)
    restarted.append(headerless, {"kind": "message", "value": 2})
    assert [record["seq"] for record in restarted.read(headerless)] == [1, 2]


def test_torn_trailing_line_does_not_break_read_or_next_append(tmp_path):
    path = tmp_path / "torn.jsonl"
    raw_log.append(path, {"kind": "message", "value": "intact"}, _HEADER)
    with open(path, "ab") as stream:
        stream.write(b'{"kind":"message","value":"torn"')

    assert [record.get("value") for record in raw_log.read(path)] == [None, "intact"]

    raw_log.append(path, {"kind": "message", "value": "after"}, _HEADER)
    records = raw_log.read(path)
    assert [record.get("value") for record in records] == [None, "intact", "after"]
    assert records[-1]["seq"] == 2


def test_unserializable_content_never_raises(tmp_path):
    path = tmp_path / "bad.jsonl"

    raw_log.append(path, {"kind": "message", "value": object()}, _HEADER)
    raw_log.append(path, {"kind": "lifecycle", "event": "finished"}, _HEADER)

    records = raw_log.read(path)
    assert records[0] == _HEADER
    assert records[1]["kind"] == "lifecycle"
    assert records[1]["seq"] == 1


def test_content_is_never_truncated(tmp_path):
    path = tmp_path / "large.jsonl"
    content = "full-argument-λ" * 20_000
    record = {
        "kind": "message",
        "message": {
            "role": "assistant",
            "content": content,
            "tool_calls": [{"name": "fs_write", "args": {"body": content}}],
        },
    }

    raw_log.append(path, record, _HEADER)

    stored = raw_log.read(path)[1]["message"]
    assert stored["content"] == content
    assert stored["tool_calls"][0]["args"]["body"] == content
