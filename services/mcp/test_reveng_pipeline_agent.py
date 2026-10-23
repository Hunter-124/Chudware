"""RE pipeline agent_fn + unstubbed MCP tools (photogrammetry real wiring).

Monkeypatches vision backends (research_ingest seam pattern) to prove:

  1. Sparse registration falls back when weak; only OpenMVS produces the CAD mesh.
  2. n_fit calls segment_fit(mesh_path, session_id=…) and persists fits.json.
  3. Deterministic parametric reconstruction publishes its document to the session.
  4. Unavailable backends degrade honestly without synthetic success.

Hermetic: no COLMAP/OpenMVS/libchudware required. Run:

  pytest services/mcp/test_reveng_pipeline_agent.py services/mcp/test_recon_hints.py -q
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest



from services.mcp import reveng_tools as rt  # noqa: E402
from services.mcp import reveng_common as rc  # noqa: E402  pipeline internals moved
from services.mcp import reveng_build_tools as rb  # noqa: E402
from services.mcp import reveng_session_tools as rs  # noqa: E402


def _patch_reveng_status(monkeypatch, value):
    """_reveng_status is looked up in common (pipeline fn) + both tool modules."""
    for mod in (rc, rb, rs):
        monkeypatch.setattr(mod, "_reveng_status", value)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def session_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point CHUDWARE_REVENG_DIR at tmp and seed a session with images + mesh."""
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path))
    sid = "re_pipe_test"
    root = tmp_path / sid
    images = root / "images"
    images.mkdir(parents=True)
    for i in range(3):
        from PIL import Image
        Image.new("RGB", (16, 16), (i * 60, 20, 40)).save(images / f"img_{i:02d}.jpg")
    mesh = root / "mesh" / "raw.ply"
    mesh.parent.mkdir(parents=True)
    mesh.write_text("ply\n")
    s = rt._make_session(sid, "pipeline test", None)
    s["artifacts"]["images"] = 3
    s["artifacts"]["mesh_path"] = str(mesh)
    s["stages"]["capture"] = "done"
    rt._save_session(s)
    return sid, root, mesh


def _install_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sparse_ok: bool = True,
    registered: int = 3,
    dense: str | None = "openmvs",
    fit_ok: bool = True,
    ml_ok: bool = True,
) -> dict:
    """Patch callable boundaries on real modules; pytest restores each attribute."""
    calls = {key: [] for key in ("sparse", "ffwd", "openmvs", "cleanup", "fit", "re", "ml")}

    def run_sparse(image_dir, out_dir, **kw):
        calls["sparse"].append({"image_dir": image_dir, "out_dir": out_dir, **kw})
        if not sparse_ok:
            return {"ok": False, "reason": "backend_not_provisioned"}
        sparse_dir = Path(out_dir) / "sparse"
        sparse_dir.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "sparse_dir": str(sparse_dir),
                "registered": registered, "backend": "pycolmap"}

    def run_ffwd_sfm(image_paths, session_dir):
        calls["ffwd"].append({"image_paths": list(image_paths), "session_dir": str(session_dir)})
        if not sparse_ok:
            return {"ok": False, "reason": "backend_not_provisioned"}
        sparse_dir = Path(session_dir) / "sparse"
        sparse_dir.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "backend": "vggt", "n_images": len(image_paths),
                "sparse_dir": str(sparse_dir)}

    def run_openmvs(sparse_dir, session_dir, **kw):
        calls["openmvs"].append({"sparse_dir": sparse_dir, "session_dir": session_dir})
        if dense != "openmvs":
            return {"ok": False, "reason": "backend_not_provisioned", "backend": "openmvs"}
        mesh = Path(session_dir) / "mesh" / "openmvs.obj"
        mesh.parent.mkdir(parents=True, exist_ok=True)
        mesh.write_text("o mesh\n")
        return {"ok": True, "mesh_path": str(mesh), "backend": "openmvs"}

    def cleanup_mesh(mesh_path, out_path, **kw):
        calls["cleanup"].append({"mesh_path": mesh_path, "out_path": out_path, **kw})
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("cleaned\n")
        return {"ok": True, "mesh_path": out_path, "n_vertices": 10, "n_triangles": 12}

    def segment_fit(mesh_path=None, *, session_id="", **kw):
        calls["fit"].append({"mesh_path": mesh_path, "session_id": session_id, **kw})
        if not fit_ok:
            return {"ok": False, "reason": "backend_not_provisioned"}
        return {
            "schema": "aicad.reveng_fits/1", "ok": True,
            "session_id": session_id, "mesh_path": mesh_path or "",
            "fits": [{
                "kind": "cylinder", "confidence": 0.9,
                "params_mm": {"radius": 8.0, "height": 12.0,
                              "axis": [0, 0, 1], "origin": [0, 0, 0]},
                "source": "ransac", "face_ids": ["f1"],
            }],
            "sections": [],
        }

    def build_parametric(mesh_path, session_dir, scale_transform=None, device=None):
        calls["ml"].append({"mesh_path": mesh_path, "session_dir": session_dir,
                            "scale_transform": scale_transform, "device": device})
        if not ml_ok:
            return {"ok": False, "error": "backend_not_provisioned",
                    "cadquery_src": None, "step_path": None}
        step = Path(session_dir) / "cad_recode.step"
        step.write_text("ISO-10303-21;\n")
        return {"ok": True, "step_path": str(step), "error": None,
                "provenance": {"method": "ml_cad_recode"}}

    def reverse_engineer(source, *, name=None, hints=None):
        calls["re"].append({"source": source, "name": name, "hints": hints})
        return {"ok": True, "doc_id": "doc_rebuilt_1", "ref_doc_id": "doc_ref_1",
                "match_pct": 92.5, "features": [], "rejected": [], "candidates": [], "notes": []}

    for target, fn in (
        ("services.vision.colmap_pipeline.run_sparse", run_sparse),
        ("services.vision.ffwd_sfm.run_ffwd_sfm", run_ffwd_sfm),
        ("services.vision.dense_backends.run_openmvs", run_openmvs),
        ("services.vision.mesh_cleanup.cleanup_mesh", cleanup_mesh),
        ("services.vision.segment_fit.segment_fit", segment_fit),
        ("services.vision.cad_recode.build_parametric", build_parametric),
    ):
        monkeypatch.setattr(target, fn)
    monkeypatch.setattr(rc, "_call_reverse_engineer", reverse_engineer)
    _patch_reveng_status(monkeypatch, lambda: {
        "ok": True,
        "capabilities": {"sparse": sparse_ok, "dense": dense == "openmvs",
                         "splat": True, "single_photo": True},
        "provisioning": {}, "banners": [], "missing": [],
    })
    return calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReconstructNode:
    def test_calls_run_sparse_dense_cleanup(self, session_env, monkeypatch):
        sid, root, _mesh = session_env
        # Drop pre-seeded mesh so reconstruct owns mesh_path
        s = rt._load_session(sid)
        s["artifacts"]["mesh_path"] = None
        rt._save_session(s)

        monkeypatch.setenv("OPENMVS_BIN", "/fake/openmvs")
        calls = _install_backend(monkeypatch, registered=3, dense="openmvs")

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("reconstruct", {"node_id": "n_reconstruct"})
        assert res.success is True, res.result
        # ffwd not needed when registration strong (>=60%)
        assert not calls["ffwd"]

        s2 = rt._load_session(sid)
        assert s2["artifacts"]["mesh_path"] == str(root / "mesh" / "cleaned.ply")
        assert Path(s2["artifacts"]["mesh_path"]).is_file()
        assert s2["artifacts"]["sparse_dir"] == str(root / "sparse")
        assert s2["stages"]["reconstruct"] == "done"

    def test_weak_registration_falls_back_to_ffwd(self, session_env, monkeypatch):
        sid, root, _mesh = session_env
        s = rt._load_session(sid)
        s["artifacts"]["mesh_path"] = None
        rt._save_session(s)

        monkeypatch.setenv("OPENMVS_BIN", "/fake/openmvs")
        # 1/3 registered = 33% < 60% → ffwd
        calls = _install_backend(monkeypatch, registered=1, dense="openmvs")

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("reconstruct", {"node_id": "n_reconstruct"})
        assert res.success is True, res.result
        assert calls["ffwd"], "weak registration must call run_ffwd_sfm"
        assert res.result.get("used_ffwd") is True

    def test_sparse_backend_failure_degrades_honestly(self, session_env, monkeypatch):
        sid, _root, _mesh = session_env
        s = rt._load_session(sid)
        s["artifacts"]["mesh_path"] = None
        rt._save_session(s)

        calls = _install_backend(monkeypatch, sparse_ok=False)

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("reconstruct", {"node_id": "n_reconstruct"})
        assert res.success is False
        assert res.result["reason"] == "backend_not_provisioned"
        assert calls["ffwd"]
        assert not calls["openmvs"]
        assert rt._load_session(sid)["artifacts"]["mesh_path"] is None

    def test_splat_capability_cannot_complete_cad_mesh_reconstruction(self, session_env, monkeypatch):
        sid, root, _mesh = session_env
        session = rt._load_session(sid)
        session["artifacts"]["mesh_path"] = None
        rt._save_session(session)
        monkeypatch.delenv("OPENMVS_BIN", raising=False)
        calls = _install_backend(monkeypatch, dense=None)
        progress = []
        fn = rt._make_pipeline_agent_fn(
            sid, progress=lambda stage, *args: progress.append(stage),
        )

        result = fn("reconstruct", {"node_id": "n_reconstruct"})

        assert result.success is False
        persisted = rt._load_session(sid)
        assert persisted["artifacts"]["sparse_dir"] == str(root / "sparse")
        assert persisted["artifacts"]["mesh_path"] is None
        assert persisted["stages"]["reconstruct"] != "done"
        assert not calls["cleanup"]
        assert "splat" not in progress


class TestFitNode:
    def test_segment_fit_persists_fits(self, session_env, monkeypatch):
        sid, root, mesh = session_env
        _install_backend(monkeypatch)

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("fit", {"node_id": "n_fit"})
        assert res.success is True, res.result
        fits_path = root / "fits.json"
        assert fits_path.is_file()
        doc = json.loads(fits_path.read_text())
        assert doc["schema"] == "aicad.reveng_fits/1"
        assert doc["fits"]

    def test_unavailable_fit_does_not_publish_fits(self, session_env, monkeypatch):
        sid, _root, _mesh = session_env
        _install_backend(monkeypatch, fit_ok=False)

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("fit", {"node_id": "n_fit"})
        assert res.success is False
        assert res.result["reason"] == "backend_not_provisioned"
        assert not (_root / "fits.json").exists()


class TestParametricNode:
    def test_deterministic_rebuild_publishes_document(
        self, session_env, monkeypatch,
    ):
        sid, root, mesh = session_env
        _install_backend(monkeypatch)
        # Pre-seed fits.json as n_fit would
        fits = {
            "schema": "aicad.reveng_fits/1",
            "ok": True,
            "session_id": sid,
            "fits": [{"kind": "cylinder", "confidence": 0.9,
                      "params_mm": {"radius": 8, "height": 12,
                                    "axis": [0, 0, 1], "origin": [0, 0, 0]}}],
        }
        (root / "fits.json").write_text(json.dumps(fits))
        s = rt._load_session(sid)
        s["artifacts"]["fits_path"] = str(root / "fits.json")
        rt._save_session(s)

        fn = rt._make_pipeline_agent_fn(sid, strategy="deterministic")
        res = fn("parametric", {"node_id": "n_parametric"})
        assert res.success is True, res.result

        s2 = rt._load_session(sid)
        assert s2["artifacts"]["doc_id"] == "doc_rebuilt_1"


    def test_unavailable_ml_does_not_publish_a_document(self, session_env, monkeypatch):
        sid, _root, _mesh = session_env
        _install_backend(monkeypatch, ml_ok=False)

        fn = rt._make_pipeline_agent_fn(sid, strategy="ml")
        res = fn("parametric", {"node_id": "n_parametric"})
        assert res.success is False
        assert res.result["error"] == "backend_not_provisioned"
        assert not rt._load_session(sid)["artifacts"].get("doc_id")


class TestMcpToolUnstubs:

    def test_single_photo_tool_tags_honesty(self, session_env, monkeypatch):
        sid, root, _mesh = session_env
        calls = {"sp": []}


        def reconstruct_single_photo(image_path, session_id, *, quality="low",
                                     output_dir=None):
            calls["sp"].append({
                "image_path": image_path, "session_id": session_id,
                "quality": quality, "output_dir": output_dir,
            })
            mesh = Path(output_dir or root / "mesh") / "single.ply"
            mesh.parent.mkdir(parents=True, exist_ok=True)
            mesh.write_text("ply\n")
            return {
                "ok": True, "session_id": session_id, "mesh_path": str(mesh),
                "backend": "triposr", "honesty": "approximate", "quality": quality,
            }

        monkeypatch.setattr("services.vision.single_photo.reconstruct_single_photo",
                            reconstruct_single_photo)
        _patch_reveng_status(monkeypatch, lambda: {
            "ok": True,
            "capabilities": {"single_photo": True},
            "provisioning": {}, "banners": [], "missing": [],
        })

        class _MCP:
            def __init__(self):
                self._tools = {}

            def tool(self):
                def deco(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return deco

        mcp = _MCP()
        rt.register_reveng_tools(mcp)
        raw = mcp._tools["reveng_single_photo"](sid, tier="triposr")
        out = json.loads(raw)
        assert out.get("ok") is True, out
        assert out.get("honesty") == "approximate"
        assert calls["sp"]
        s = rt._load_session(sid)
        assert s["honesty"]["approximate"] is True
        assert s["artifacts"]["mesh_path"]
