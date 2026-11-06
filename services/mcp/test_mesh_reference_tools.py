"""Private-hostd integration coverage for embedded scan reference tools."""
import asyncio
import json

import pytest

from services.mcp import core_bridge as cb


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
    path = tmp_path / "scan.ply"
    path.write_text("""ply
format ascii 1.0
element vertex 4
property float x
property float y
property float z
element face 4
property list uchar int vertex_indices
end_header
0 0 0
1 0 0
0 2 0
0 0 3
3 0 2 1
3 0 1 3
3 0 3 2
3 1 2 3
""", encoding="ascii")
    return path


def call(tool_name, /, **args):
    from services.mcp.server import mcp
    _, structured = asyncio.run(mcp.call_tool(tool_name, args))
    return json.loads(structured["result"])


@pytest.fixture
def doc():
    result = call("new_part", name="MeshReferenceTest", fresh=True)
    assert result["ok"], result
    yield result["doc_id"]
    cb.call("doc_close", {"doc_id": result["doc_id"]})


def test_reference_tools_native_roundtrip(doc, mesh):
    transform = {"mm_per_unit": 2.0, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                 "translation_mm": [10, 20, 30]}
    added = call("add_mesh_reference", doc_id=doc, path=str(mesh), name="scan",
                 transform=transform, opacity=0.4)
    assert added["ok"], added
    assert added["triangles"] == 4
    assert added["bbox_mm"]["min"] == pytest.approx([10, 20, 30])
    assert added["bbox_mm"]["max"] == pytest.approx([12, 24, 36])
    rid = added["id"]
    listed = call("list_mesh_references", doc_id=doc)
    assert listed["ok"] and any(ref["id"] == rid for ref in listed["refs"])
    updated = call("update_mesh_reference", doc_id=doc, id=rid, visible=False,
                   color=[0.2, 0.3, 0.4], name="hidden")
    assert updated["ok"], updated
    ref = next(ref for ref in call("list_mesh_references", doc_id=doc)["refs"] if ref["id"] == rid)
    assert ref["visible"] is False and ref["name"] == "hidden"
    points = cb.call("doc_mesh_ref_mesh", {"doc_id": doc, "id": rid})
    assert points["ok"] and len(points["vertices_mm"]) >= 12 and len(points["indices"]) == 12
    removed = call("remove_mesh_reference", doc_id=doc, id=rid)
    assert removed["ok"], removed
    assert not call("list_mesh_references", doc_id=doc)["refs"]


@pytest.mark.parametrize("confirmed,approximate,applied", [(False, False, False),
    (False, True, False), (True, False, False), (True, False, True)])
def test_attach_session_reference(doc, mesh, monkeypatch, confirmed, approximate, applied):
    from services.mcp import reveng_common
    session = {"session_id": "re_test", "artifacts": {"mesh_path": str(mesh),
               "mesh_transform_applied": applied}, "honesty": {},
               "recon_transform": {"mm_per_unit": 2.0, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
               "translation_mm": [10, 20, 30], "confirmed": confirmed, "source": "test"}}
    monkeypatch.setattr(reveng_common, "_load_session", lambda *args, **kwargs: session)
    result = call("reveng_attach_reference", session_id="re_test", doc_id=doc, approximate=approximate)
    if not confirmed and not approximate:
        assert not result["ok"] and result["reason"] == "scale_unconfirmed"
        return
    assert result["ok"], result
    ref = next(r for r in call("list_mesh_references", doc_id=doc)["refs"] if r["id"] == result["id"])
    assert ref["approximate"] == approximate
    assert ref["provenance"]["reveng_session"] == "re_test"
    if approximate:
        assert "approximate" in ref["name"].lower()
    assert result["bbox_mm"]["min"] == pytest.approx([0, 0, 0] if applied else [10, 20, 30])


def test_deviation_accepts_embedded_query_and_reference(doc, mesh):
    pytest.importorskip("scipy")
    pytest.importorskip("trimesh")
    from services.vision.deviation import compute_deviation
    added = call("add_mesh_reference", doc_id=doc, path=str(mesh))
    assert added["ok"], added
    result = compute_deviation(None, mesh_ref_doc_id=doc, query_mesh_ref_id=added["id"],
                               ref_mesh_ref_id=added["id"], n_samples=100)
    assert result["ok"], result
    assert result["rms_mm"] >= 0


def test_worker_attach_reference_native(doc, mesh, monkeypatch):
    from services.mcp import reveng_common
    from services.reveng.worker_rpc import REVENG_WORKER_METHODS
    session = {"session_id": "re_test", "artifacts": {"mesh_path": str(mesh)},
               "honesty": {}, "recon_transform": {
                   "mm_per_unit": 1, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                   "translation_mm": [0, 0, 0], "source": "test", "confirmed": True}}
    monkeypatch.setattr(reveng_common, "_load_session", lambda *args, **kwargs: session)
    result = REVENG_WORKER_METHODS["reveng_attach_reference"](
        {"session_id": "re_test", "doc_id": doc}, lambda _: None)
    assert isinstance(result, dict) and result["ok"], result
    assert any(ref["id"] == result["id"] for ref in call("list_mesh_references", doc_id=doc)["refs"])


def test_measure_mesh_reference_native(doc, mesh):
    pytest.importorskip("scipy")
    pytest.importorskip("trimesh")
    result = call("add_features", doc_id=doc, ops=[
        cb.feat("box", "Block", {"length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10)})])
    assert result["ok"], result
    added = call("add_mesh_reference", doc_id=doc, path=str(mesh))
    assert added["ok"], added
    measured = call("measure_mesh_reference", doc_id=doc, id=added["id"], n_samples=100)
    assert measured["ok"], measured


def test_reference_saved_without_source_and_cached_part_reopens(doc, mesh, tmp_path):
    built = call("add_features", doc_id=doc, ops=[
        cb.feat("box", "Block", {"length": cb.dim(10), "width": cb.dim(10), "height": cb.dim(10)})])
    assert built["ok"], built
    added = call("add_mesh_reference", doc_id=doc, path=str(mesh))
    assert added["ok"], added
    path = str(tmp_path / "part.step+")
    saved = cb.call("doc_save", {"doc_id": doc, "path": path})
    assert saved.get("ok", True), saved
    mesh.unlink()
    opened = cb.call("doc_open", {"path": path, "detach_path": True})
    assert opened["cache"]["features_executed"] == 0, opened
    reopened = opened["doc_id"]
    try:
        listed = call("list_mesh_references", doc_id=reopened)
        assert any(ref["id"] == added["id"] for ref in listed["refs"]), listed
        data = cb.call("doc_mesh_ref_mesh", {"doc_id": reopened, "id": added["id"]})
        assert data["ok"] and len(data["indices"]) == 12
    finally:
        cb.call("doc_close", {"doc_id": reopened})


def test_reference_only_save_export_and_retirement(doc, mesh, tmp_path):
    from services.mcp.cad_common import _needs_project_save
    added = call("add_mesh_reference", doc_id=doc, path=str(mesh))
    assert added["ok"], added
    entry = next(row for row in cb.call("doc_list", {})["docs"] if row["doc_id"] == doc)
    assert entry["empty"] is False
    assert _needs_project_save(entry)
    saved = call("save_part", doc_id=doc, path=str(tmp_path / "reference.step+"))
    assert saved["ok"], saved
    exported = call("export_part", doc_id=doc, fmt="step+",
                    path=str(tmp_path / "reference-export.step+"))
    assert exported["ok"], exported
    mesh.unlink()
    for native in (saved, exported):
        reopened = cb.call("doc_open", {"path": native["path"], "detach_path": True})["doc_id"]
        try:
            listed = call("list_mesh_references", doc_id=reopened)
            assert any(ref["id"] == added["id"] for ref in listed["refs"]), listed
            data = cb.call("doc_mesh_ref_mesh", {"doc_id": reopened, "id": added["id"]})
            assert data["ok"] and len(data["indices"]) == 12, data
        finally:
            cb.call("doc_close", {"doc_id": reopened})
    stl = tmp_path / "reference.stl"
    refused = call("export_part", doc_id=doc, fmt="stl", path=str(stl))
    assert not refused["ok"] and not stl.exists(), refused


def test_reference_only_save_and_export_surface_reference_list_errors(doc, mesh, tmp_path,
                                                                      monkeypatch):
    added = call("add_mesh_reference", doc_id=doc, path=str(mesh))
    assert added["ok"], added
    real_call = cb.call

    def failing_reference_list(method, params=None):
        if method == "doc_mesh_ref_list":
            raise cb.CoreError("reference list unavailable", code="reference_unavailable")
        return real_call(method, params)

    with monkeypatch.context() as patch:
        patch.setattr(cb, "call", failing_reference_list)
        for tool, options in (("save_part", {}), ("export_part", {"fmt": "step+"})):
            destination = tmp_path / f"{tool}.step+"
            result = call(tool, doc_id=doc, path=str(destination), **options)
            assert not result["ok"] and result["code"] == "reference_unavailable", result
            assert "reference list unavailable" in result["error"], result
            assert not destination.exists()


@pytest.mark.parametrize("rows", [[], [{"doc_id": "doc-cpp1"}]])
def test_reference_guard_does_not_guess_an_unknown_document_kind(monkeypatch, rows):
    from services.mcp.cad_common import _part_has_mesh_references

    def doc_list_only(method, params=None):
        assert method == "doc_list"
        return {"docs": rows}

    monkeypatch.setattr(cb, "call", doc_list_only)
    with pytest.raises(cb.CoreError):
        _part_has_mesh_references("doc-cpp1")
