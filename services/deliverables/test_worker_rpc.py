"""Tests for the worker-RPC preview bridge (``services.deliverables.worker_rpc``).

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

from services.deliverables import api, project as deliverables_project, registry, worker_rpc
from services.deliverables.errors import DeliverableError


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CHUDWARE_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    return tmp_path


def _emit(_data):  # worker emit_event sink — unused by these methods
    pass

def _stub_report(monkeypatch, build):
    class _Template:
        def build(self, resolved, fmt, out_path):
            build(resolved, fmt, out_path)

    monkeypatch.setattr(api, "get_template", lambda _name, _kind: _Template())
    monkeypatch.setattr(
        api,
        "validate_and_resolve",
        lambda _spec, _kind, _template, _refs: {"title": "Stub report", "blocks": []},
    )
    return {"kind": "report", "template": "stub", "outline": []}


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
    from services.deliverables import registry

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


def test_make_rejects_builder_that_writes_nothing(project, monkeypatch):
    spec = _stub_report(monkeypatch, lambda _resolved, _fmt, _out_path: None)

    env = api.make_report(spec)

    assert env["ok"] is False
    assert env["reason"] == "artifact_write_failed"
    assert api.list_artifacts()["count"] == 0
    assert not deliverables_project.registry_path().exists()


def test_make_classifies_missing_runtime_dependency(project, monkeypatch):
    def missing_runtime(_resolved, _fmt, _out_path):
        raise ModuleNotFoundError("No module named 'document_engine'")

    env = api.make_report(_stub_report(monkeypatch, missing_runtime))

    assert env["ok"] is False
    assert env["reason"] == "runtime_unavailable"
    assert env["reason"] != "invalid_spec"
    assert api.list_artifacts()["count"] == 0


def test_make_classifies_permission_error_writing_spec(project, monkeypatch):
    spec = _stub_report(
        monkeypatch,
        lambda _resolved, _fmt, out_path: out_path.write_bytes(b"real artifact"),
    )
    original_write_text = Path.write_text

    def deny_spec(self, *args, **kwargs):
        if self.name.endswith(".spec.json"):
            raise PermissionError("spec path is read-only")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", deny_spec)
    env = api.make_report(spec)

    assert env["ok"] is False
    assert env["reason"] == "artifact_write_failed"
    assert env["reason"] != "invalid_spec"
    assert not deliverables_project.registry_path().exists()


def test_preview_classifies_missing_rasterizer_dependency(project, monkeypatch):
    artifact = deliverables_project.artifacts_dir() / "preview.pdf"
    artifact.write_bytes(b"%PDF-placeholder")
    monkeypatch.setattr(
        api.engines,
        "pdf_to_pngs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ModuleNotFoundError("No module named 'pypdfium2'")
        ),
    )

    env = api.render_document(str(artifact))

    assert env["ok"] is False
    assert env["reason"] == "converter_missing"
    assert env["reason"] != "artifact_not_found"


def test_unset_project_root_is_shaped_and_creates_no_fallback(tmp_path, monkeypatch):
    app_root = tmp_path / "appdata"
    monkeypatch.delenv("CHUDWARE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CHUDWARE_ARTIFACTS_DIR", raising=False)
    monkeypatch.setattr(deliverables_project, "_APP_ROOT", app_root)
    spec = _stub_report(
        monkeypatch,
        lambda _resolved, _fmt, out_path: out_path.write_bytes(b"artifact"),
    )

    made = api.make_report(spec)
    listed = api.list_artifacts()

    assert made["ok"] is False and made["reason"] == "project_root_unavailable"
    assert listed["ok"] is False and listed["reason"] == "project_root_unavailable"
    assert not (app_root / "project").exists()
    assert not app_root.exists()


def test_malformed_registry_is_never_rewritten(project):
    path = deliverables_project.registry_path()
    seed = b'{"truncated":'
    path.write_bytes(seed)

    listed = api.list_artifacts()
    with pytest.raises(DeliverableError) as exc_info:
        registry.append_record({"ok": True, "artifact_id": "must-not-land"})

    assert listed["ok"] is False and listed["reason"] == "registry_corrupt"
    assert exc_info.value.reason == "registry_corrupt"
    assert path.read_bytes() == seed
