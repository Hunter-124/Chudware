"""Tests for EventJournal — contracts/agent_events.md §1, §5."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from services.dag.event_journal import EventJournal, run_events_path


def test_emit_writes_line(tmp_path: Path) -> None:
    """Each emit() call writes exactly one JSON line to events.jsonl."""
    j = EventJournal("run-test-001", runs_dir=tmp_path)
    j.emit("dag.run.started", {"graph_id": "g1", "node_count": 2, "graph_name": "Test"})
    j.emit("dag.node.started", {"node_id": "n1", "node_type": "design", "node_name": "Design"})

    path = j.path
    assert path.exists(), "events.jsonl must be created"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

    first = json.loads(lines[0])
    assert first["kind"] == "dag.run.started"
    assert first["run_id"] == "run-test-001"
    assert "ts" in first
    assert first["graph_id"] == "g1"

    second = json.loads(lines[1])
    assert second["kind"] == "dag.node.started"
    assert second["node_id"] == "n1"


def test_wrap_calls_both(tmp_path: Path) -> None:
    """wrap() calls the journal AND the original emitter."""
    j = EventJournal("run-test-002", runs_dir=tmp_path)
    captured: list[tuple[str, dict]] = []

    def original(kind: str, payload: dict) -> None:
        captured.append((kind, payload))

    composite = j.wrap(original)
    composite("dag.node.done", {"node_id": "n1", "output_summary": "done"})

    # Journal file
    lines = [ln for ln in j.path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "dag.node.done"

    # Original emitter
    assert len(captured) == 1
    assert captured[0][0] == "dag.node.done"


def test_wrap_none(tmp_path: Path) -> None:
    """wrap(None) returns a callable that only writes to the journal."""
    j = EventJournal("run-test-003", runs_dir=tmp_path)
    emit = j.wrap(None)
    emit("dag.run.done", {"status": "done", "node_states": {}})
    lines = [ln for ln in j.path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "done"


def test_append_mode(tmp_path: Path) -> None:
    """Multiple emit() calls append to the same file, not overwrite."""
    j = EventJournal("run-test-004", runs_dir=tmp_path)
    for i in range(5):
        j.emit("dag.node.started", {"node_id": f"n{i}", "node_type": "design", "node_name": f"Node{i}"})
    lines = [ln for ln in j.path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 5


def test_run_events_path_creates_dir(tmp_path: Path) -> None:
    """run_events_path() creates the run directory if it doesn't exist."""
    p = run_events_path("new-run-abc", runs_dir=tmp_path)
    assert p.parent.is_dir()
    assert p.name == "events.jsonl"


@pytest.mark.parametrize("run_id", [".", "..", "../escape", r"..\escape", "/tmp/escape"])
def test_run_events_path_rejects_unsafe_ids_before_writing(
    tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(ValueError, match="run_id"):
        run_events_path(run_id, runs_dir=tmp_path / "runs")

    assert not (tmp_path / "runs").exists()


def test_unknown_fields_tolerated(tmp_path: Path) -> None:
    """Consumers must tolerate unknown fields (contracts/agent_events.md §5)."""
    j = EventJournal("run-test-005", runs_dir=tmp_path)
    # Emit with extra unknown field
    j.emit("dag.node.done", {"node_id": "n1", "output_summary": "ok", "future_field": 42})
    line = json.loads(j.path.read_text().splitlines()[0])
    # Extra field must be present (written as-is, not stripped)
    assert line["future_field"] == 42
