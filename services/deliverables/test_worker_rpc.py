"""Tests for the T-F16 worker-RPC preview bridge (``deliverables.worker_rpc``).

The bridge exposes ``render_document`` + ``list_artifacts`` as engine-worker methods so the ImGui
GUI reaches them over the standard worker seam. These tests prove:
  (a) the WORKER_METHODS table shape (name → callable(params, emit) → dict);
  (b) each method returns the api envelope contract shape plus an absolute ``project_dir``;
  (c) render_document degrades cleanly (``artifact_not_found``) on a missing path — still shaped;
  (d) list_artifacts round-trips the registry the GUI reads for artifact cards.

No LibreOffice / pandoc needed — missing-path and registry paths are all in-process.
Run:  python3 -m pytest services/deliverables/test_worker_rpc.py -q
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from deliverables import worker_rpc


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("AICAD_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _emit(_data):  # worker emit_event sink — unused by these methods
    pass


def test_worker_methods_table():
    m = worker_rpc.WORKER_METHODS
    assert set(m) == {"render_document", "list_artifacts"}
    assert all(callable(fn) for fn in m.values())


def test_render_document_missing_path_is_shaped(project):
    env = worker_rpc.WORKER_METHODS["render_document"]({"path": "artifacts/nope.docx"}, _emit)
    assert env["ok"] is False
    assert env["reason"] == "artifact_not_found"
    # bridge still supplies the absolute project dir so the GUI can resolve any relative paths
    assert Path(env["project_dir"]).resolve() == project.resolve()


def test_list_artifacts_roundtrip_with_project_dir(project):
    env = worker_rpc.WORKER_METHODS["list_artifacts"]({}, _emit)
    assert env["ok"] is True
    assert env["count"] == 0
    assert env["artifacts"] == []
    assert Path(env["project_dir"]).resolve() == project.resolve()


def test_list_artifacts_sees_appended_record(project):
    # A record written by a make_* tool must surface for the GUI's artifact cards.
    from deliverables import registry

    rec = registry.build_record(
        "art_report_TESTULID", "report",
        project / "artifacts" / "demo.docx", "run_test", [],
        project / "artifacts" / "art_report_TESTULID.spec.json")
    registry.append_record(rec)

    env = worker_rpc.WORKER_METHODS["list_artifacts"]({"run_id": "run_test"}, _emit)
    assert env["ok"] is True
    assert env["count"] == 1
    assert env["artifacts"][0]["artifact_id"] == "art_report_TESTULID"
    assert env["artifacts"][0]["kind"] == "report"
