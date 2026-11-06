"""Regression coverage for honest reconstruction and downstream state invalidation."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.mcp import recon_tools, reveng_build_tools, reveng_common


class CaptureMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "reveng"))
    session = reveng_common._make_session("re_test", "test", None)
    reveng_common._session_path("re_test", create=True).mkdir(parents=True, exist_ok=True)
    mesh = tmp_path / "old.obj"
    mesh.write_text("v 0 0 0\n")
    session["artifacts"].update(mesh_path=str(mesh), raw_mesh_path=str(mesh),
                                doc_id="old-doc", fits_path="old-fits")
    session["result"] = {"state": "built", "doc_id": "old-doc", "match_pct": 95,
                         "scored": True}
    from services.vision.recon_transform import ReconTransform
    session["recon_transform"] = ReconTransform.identity().to_json()
    session["stages"]["model"] = "done"
    reveng_common._save_session(session)
    mcp = CaptureMCP()
    reveng_build_tools.register_reveng_build_tools(mcp)
    return mcp.tools, session


def test_empty_reconstruction_retains_bounded_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path))
    faces = [{"sid": f"face-{i}", "surf": "plane"} for i in range(250)]
    plan = {"kind": "mesh", "base": None, "holes": [], "patterns": [],
            "fillet_candidates": [], "chamfer_candidates": [], "notes": [],
            "face_count": len(faces), "bbox_mm": [1, 1, 1], "unclaimed": faces}
    monkeypatch.setattr(recon_tools, "_doc", lambda source: source)
    monkeypatch.setattr(recon_tools, "_query_faces", lambda doc: faces)
    monkeypatch.setattr(recon_tools, "_recognize", lambda doc: plan)
    def call(method, params):
        if method == "doc_new":
            return {"doc_id": "empty"}
        if method == "doc_tree":
            return {"tree": {"name": "source"}}
        if method == "doc_close":
            assert params["doc_id"] == "empty"
            return {"ok": True}
        raise AssertionError(method)
    monkeypatch.setattr(recon_tools.cb, "call", call)
    mcp = CaptureMCP()
    recon_tools.register_recon_tools(mcp)
    out = json.loads(mcp.tools["reverse_engineer"]("source"))
    assert out["ok"] is False and out["reason"] == "no_features_rebuilt"
    assert out["features"] == [] and out["match_pct"] is None
    evidence = out["recognition"]
    assert evidence["unclaimed_count"] == len(faces)
    assert evidence["unclaimed"] == faces[:100]
    assert json.loads(Path(evidence["unclaimed_path"]).read_text()) == faces


def test_applied_features_without_bodies_are_not_built(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path))
    face = {"sid": "face-1", "surf": "plane"}
    plan = {"kind": "prismatic", "face_count": 1, "bbox_mm": [1, 1, 1],
            "base": {"type": "extrude", "entities": [], "offset": 0,
                     "plane": "plane.top", "depth": 1, "region_centroid": [0, 0]},
            "holes": [], "patterns": [], "fillet_candidates": [], "chamfer_candidates": [],
            "notes": [], "unclaimed": [face]}
    monkeypatch.setattr(recon_tools, "_doc", lambda source: source)
    monkeypatch.setattr(recon_tools, "_query_faces", lambda doc: [face])
    monkeypatch.setattr(recon_tools, "_recognize", lambda doc: plan)
    monkeypatch.setattr(recon_tools, "_feature_ok", lambda result: (True, []))
    monkeypatch.setattr(recon_tools, "_compare", lambda *args: {"match_pct": 50.0})
    def call(method, params):
        if method == "doc_new":
            return {"doc_id": "empty"}
        if method == "doc_tree":
            return {"tree": {"name": "source"}}
        if method == "doc_checkpoint":
            return {"checkpoint": "before"}
        if method == "doc_apply_ops":
            return {"id_map": {"$1": "feature-1"}}
        if method == "asm_world_bodies":
            return {"bodies": []}
        if method == "doc_close":
            return {"ok": True}
        raise AssertionError(method)
    monkeypatch.setattr(recon_tools.cb, "call", call)
    mcp = CaptureMCP()
    recon_tools.register_recon_tools(mcp)
    out = json.loads(mcp.tools["reverse_engineer"]("source"))
    assert not out["ok"] and out["reason"] == "no_features_rebuilt"
    assert out["features"] and out["doc_id"] is None and out["match_pct"] is None
    assert "produced no bodies" in out["summary"]
    assert out["recognition"]["unclaimed_count"] == 1


def test_failed_direct_build_does_not_restore_stale_success(workspace, monkeypatch):
    tools, _ = workspace
    payload = {"ok": True, "reason": "no_features_rebuilt", "match_pct": 0.0,
               "doc_id": "stale", "recognition": {"unclaimed_count": 42}}
    monkeypatch.setattr(reveng_build_tools, "_make_pipeline_agent_fn",
                        lambda *a, **k: lambda *a, **k: SimpleNamespace(success=False, result=payload))
    out = json.loads(tools["reveng_build_parametric"]("re_test"))
    assert out["ok"] is False and out["reason"] == "no_features_rebuilt"
    assert out["recognition"]["unclaimed_count"] == 42
    assert out["match_pct"] is None and out["scored"] is False
    session = reveng_common._load_session("re_test")
    assert session["result"]["state"] == "failed"
    assert session["result"]["doc_id"] is None
    assert session["result"]["mesh_fallback"] is False
    assert not session["artifacts"].get("doc_id")


def test_success_envelope_with_no_features_is_failure(workspace, monkeypatch):
    tools, _ = workspace
    payload = {"ok": True, "doc_id": "empty", "features": [], "match_pct": 0.0,
               "recognition": {"unclaimed_count": 42, "unclaimed": []}}
    monkeypatch.setattr(reveng_build_tools, "_make_pipeline_agent_fn",
                        lambda *a, **k: lambda *a, **k: SimpleNamespace(success=True, result=payload))
    out = json.loads(tools["reveng_build_parametric"]("re_test"))
    assert not out["ok"] and out["reason"] == "no_features_rebuilt"
    assert out["recognition"]["unclaimed_count"] == 42
    assert out["match_pct"] is None and not out["scored"]
    saved = reveng_common._load_session("re_test")
    assert saved["result"]["state"] == "failed" and not saved["artifacts"].get("doc_id")


def test_reimport_invalidates_prior_model_and_fits(workspace, tmp_path):
    tools, _ = workspace
    replacement = tmp_path / "replacement.obj"
    replacement.write_text("v 1 0 0\n")
    out = json.loads(tools["import_scan_mesh"]("re_test", str(replacement), scale_mm=1))
    assert out["ok"] is True
    session = reveng_common._load_session("re_test")
    assert not session["artifacts"].get("fits_path")
    assert not session["artifacts"].get("doc_id")
    assert not (session.get("result") or {}).get("scored")
    assert session["stages"]["model"] != "done"


def test_reimport_same_filename_keeps_previous_mesh_artifact(workspace, tmp_path):
    tools, _ = workspace
    source = tmp_path / "scan.obj"
    source.write_text("v 1 0 0\n")
    first = json.loads(tools["import_scan_mesh"]("re_test", str(source)))
    assert first["ok"]
    source.write_text("v 2 0 0\n")
    second = json.loads(tools["import_scan_mesh"]("re_test", str(source)))
    assert second["ok"] and second["mesh_path"] != first["mesh_path"]
    assert Path(first["mesh_path"]).read_text() == "v 1 0 0\n"
    assert Path(second["mesh_path"]).read_text() == "v 2 0 0\n"


def test_bundle_names_current_import_and_preserves_previous_files(workspace, tmp_path, monkeypatch):
    import zipfile
    tools, _ = workspace
    source = tmp_path / "scan.obj"
    source.write_text("v 1 0 0\n")
    first = json.loads(tools["import_scan_mesh"]("re_test", str(source)))
    source.write_text("v 2 0 0\n")
    second = json.loads(tools["import_scan_mesh"]("re_test", str(source)))
    assert first["ok"] and second["ok"]
    root = reveng_common._session_path("re_test")
    (root / "mesh" / "scan.obj").write_text("v 0 0 0\n")
    destination = tmp_path / "bundle.zip"
    out = json.loads(tools["export_reveng_bundle"]("re_test", str(destination)))
    assert out["ok"]
    with zipfile.ZipFile(destination) as archive:
        manifest = json.loads(archive.read("session.json"))
        assert manifest["artifacts"]["mesh_path"] == "mesh/scan.obj"
        assert manifest["artifacts"]["raw_mesh_path"] == "mesh/scan.obj"
        assert archive.read(manifest["artifacts"]["mesh_path"]) == b"v 2 0 0\n"
        assert archive.read(Path(first["mesh_path"]).relative_to(root).as_posix()) == b"v 1 0 0\n"
        assert archive.read("mesh/_archive_previous_1/scan.obj") == b"v 0 0 0\n"
        assert len(archive.namelist()) == len(set(archive.namelist()))
        archive.extractall(tmp_path / "restored" / "re_test")
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "restored"))
    restored = reveng_common._load_session("re_test", merge_gaussian=False)
    assert Path(restored["artifacts"]["mesh_path"]).read_text() == "v 2 0 0\n"
    second_bundle = tmp_path / "bundle-again.zip"
    result = json.loads(tools["export_reveng_bundle"]("re_test", str(second_bundle)))
    assert result["ok"]
    with zipfile.ZipFile(second_bundle) as archive:
        assert archive.read("mesh/scan.obj") == b"v 2 0 0\n"


def test_direct_fit_persists_provenance_and_clears_model(workspace, monkeypatch):
    from services.vision import segment_fit
    tools, _ = workspace
    monkeypatch.setattr(segment_fit, "segment_fit", lambda *a, **k: {"ok": True, "fits": []})
    out = json.loads(tools["reveng_fit_primitives"]("re_test"))
    assert out["ok"] is True and out["provenance"]
    session = reveng_common._load_session("re_test")
    persisted = json.loads(Path(session["artifacts"]["fits_path"]).read_text())
    assert persisted["provenance"] == out["provenance"]
    assert not session["artifacts"].get("doc_id")
    assert not (session.get("result") or {}).get("scored")


def test_fit_rejects_mesh_changed_during_segmentation(workspace, monkeypatch):
    from services.vision import segment_fit
    tools, session = workspace
    def change_mesh(*args, **kwargs):
        Path(session["artifacts"]["raw_mesh_path"]).write_text("v 7 0 0\n")
        return {"ok": True, "fits": []}
    monkeypatch.setattr(segment_fit, "segment_fit", change_mesh)
    out = json.loads(tools["reveng_fit_primitives"]("re_test"))
    assert out["ok"] is False and out["reason"] == "mesh_changed_during_fit"
    current = reveng_common._load_session("re_test")
    assert not current["artifacts"].get("doc_id")
    assert "result" not in current


def test_approximate_mesh_attachment_is_not_a_mesh_body(workspace):
    _, session = workspace
    session["artifacts"].pop("doc_id")
    session["honesty"]["approximate"] = True
    session["result"] = {}
    reveng_build_tools._attach_session_result(session, "test:part")
    sidecar = json.loads((reveng_common._session_path("re_test") /
                          "reveng_result.json").read_text())
    assert sidecar["geometry_source"] == "mesh"
    assert sidecar["mesh_fallback"] is False


def test_single_photo_replacement_invalidates_prior_model(workspace, monkeypatch, tmp_path):
    from services.vision import single_photo
    tools, _ = workspace
    mesh = tmp_path / "new.obj"
    mesh.write_text("v 1 0 0\n")
    monkeypatch.setattr(reveng_build_tools, "_session_image_paths", lambda sid: ["photo.png"])
    monkeypatch.setattr(single_photo, "reconstruct_single_photo",
                        lambda *a, **k: {"ok": True, "mesh_path": str(mesh)})
    out = json.loads(tools["reveng_single_photo"]("re_test"))
    assert out["ok"] is True
    session = reveng_common._load_session("re_test")
    assert session["artifacts"]["raw_mesh_path"] == str(mesh)
    assert not session["artifacts"].get("doc_id")
    assert not session["artifacts"].get("fits_path")
    assert not (session.get("result") or {}).get("scored")
