"""RE pipeline agent_fn + unstubbed MCP tools (Wave-1 photogrammetry real wiring).

Monkeypatches vision backends (RE-34 research_ingest seam pattern) to prove:

  1. n_reconstruct calls run_sparse → (weak reg) run_ffwd_sfm → dense → cleanup_mesh
     and writes session artifacts.mesh_path / splat_path.
  2. n_fit calls segment_fit(mesh_path, session_id=…) and persists fits.json.
  3. n_parametric deterministic calls reverse_engineer with hints=fits.
  4. n_parametric strategy='ml' calls cad_recode.build_parametric with the agreed
     signature (mesh_path, session_dir, scale_transform).
  5. Missing imports degrade honestly (ok=False / backend_not_provisioned) — never
     a synthetic success.

Hermetic: no COLMAP/OpenMVS/libaicad required. Run:

  pytest services/mcp/test_reveng_pipeline_agent.py services/mcp/test_recon_hints.py -q
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Lightweight import stubs (mirrors test_reveng_tools / test_reveng_missions)
# ---------------------------------------------------------------------------

def _ensure_import_stubs() -> None:
    try:
        import mcp.server.fastmcp  # noqa: F401
    except Exception:  # noqa: BLE001
        if "mcp" not in sys.modules:
            mcp_pkg = types.ModuleType("mcp")
            server_mod = types.ModuleType("mcp.server")
            fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

            class _FakeFastMCP:
                def __init__(self, name="", **kw):
                    self._tools = {}
                    self._tool_manager = types.SimpleNamespace(
                        get_tool=lambda n: types.SimpleNamespace(
                            fn=self._tools.get(n)
                        )
                    )

                def tool(self):
                    def decorator(fn):
                        self._tools[fn.__name__] = fn
                        return fn
                    return decorator

            fastmcp_mod.FastMCP = _FakeFastMCP
            sys.modules["mcp"] = mcp_pkg
            sys.modules["mcp.server"] = server_mod
            sys.modules["mcp.server.fastmcp"] = fastmcp_mod

    if "mcp_server.core_bridge" not in sys.modules:
        cb = types.ModuleType("mcp_server.core_bridge")
        cb.available = lambda: False
        cb.call = lambda method, params=None: {}

        class CoreError(Exception):
            pass

        cb.CoreError = CoreError
        sys.modules["mcp_server.core_bridge"] = cb
        sys.modules.setdefault("core_bridge", cb)

    if "mcp_server.cad_tools" not in sys.modules:
        ct = types.ModuleType("mcp_server.cad_tools")
        ct._dumps = lambda obj: json.dumps(obj)
        ct._err = lambda e: json.dumps({"ok": False, "error": str(e)})
        ct._CUR = {}
        ct._doc = lambda x: x
        ct._publish_current = lambda *a, **k: None
        ct._read_prompt = lambda *a, **k: ""
        sys.modules["mcp_server.cad_tools"] = ct


_ensure_import_stubs()

from services.mcp import reveng_tools as rt  # noqa: E402
from services.mcp import reveng_common as rc  # noqa: E402  T-RF8: pipeline internals moved
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
    """Point AICAD_REVENG_DIR at tmp and seed a session with images + mesh."""
    monkeypatch.setenv("AICAD_REVENG_DIR", str(tmp_path))
    sid = "re_pipe_test"
    root = tmp_path / sid
    images = root / "images"
    images.mkdir(parents=True)
    for i in range(3):
        (images / f"img_{i:02d}.jpg").write_bytes(b"\xff\xd8\xff fakejpeg")
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
    re_ok: bool = True,
    ml_ok: bool = True,
    fail_imports: set[str] | None = None,
) -> dict:
    """Install fake vision modules; return call log."""
    fail_imports = fail_imports or set()
    calls: dict = {"sparse": [], "ffwd": [], "openmvs": [], "brush": [],
                   "cleanup": [], "fit": [], "re": [], "ml": []}

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    # colmap_pipeline
    if "colmap_pipeline" in fail_imports:
        monkeypatch.setitem(
            sys.modules, "engine_oracle.vision.colmap_pipeline",
            types.ModuleType("broken"),
        )
        # Make import raise
        class _Boom:
            def __getattr__(self, _n):
                raise ImportError("colmap missing")
        monkeypatch.setitem(sys.modules, "engine_oracle.vision.colmap_pipeline", _Boom())  # type: ignore
    else:
        cp = _mod("engine_oracle.vision.colmap_pipeline")

        def run_sparse(image_dir, out_dir, **kw):
            calls["sparse"].append({"image_dir": image_dir, "out_dir": out_dir, **kw})
            if sparse_ok:
                sparse_dir = str(Path(out_dir) / "sparse")
                Path(sparse_dir).mkdir(parents=True, exist_ok=True)
                return {"ok": True, "sparse_dir": sparse_dir,
                        "registered": registered, "backend": "pycolmap"}
            return {"ok": False, "reason": "backend_not_provisioned",
                    "hint": "pip install pycolmap"}

        cp.run_sparse = run_sparse

    # ffwd_sfm
    ff = _mod("engine_oracle.vision.ffwd_sfm")

    def run_ffwd_sfm(image_paths, session_dir):
        calls["ffwd"].append({"image_paths": list(image_paths),
                              "session_dir": str(session_dir)})
        sparse_dir = str(Path(session_dir) / "sparse")
        Path(sparse_dir).mkdir(parents=True, exist_ok=True)
        return {"ok": True, "backend": "vggt", "n_images": len(image_paths),
                "sparse_dir": sparse_dir}

    ff.run_ffwd_sfm = run_ffwd_sfm

    # dense_backends
    db = _mod("engine_oracle.vision.dense_backends")

    def run_openmvs(sparse_dir, session_dir, **kw):
        calls["openmvs"].append({"sparse_dir": sparse_dir, "session_dir": session_dir})
        if dense != "openmvs":
            return {"ok": False, "reason": "backend_not_provisioned",
                    "backend": "openmvs"}
        mesh = Path(session_dir) / "mesh" / "openmvs.obj"
        mesh.parent.mkdir(parents=True, exist_ok=True)
        mesh.write_text("o mesh\n")
        return {"ok": True, "mesh_path": str(mesh), "backend": "openmvs"}

    def run_brush(sparse_dir, session_dir, **kw):
        calls["brush"].append({"sparse_dir": sparse_dir, "session_dir": session_dir})
        if dense != "brush":
            return {"ok": False, "reason": "backend_not_provisioned", "backend": "brush"}
        splat = Path(session_dir) / "splat" / "splat.ply"
        mesh = Path(session_dir) / "mesh" / "brush.ply"
        splat.parent.mkdir(parents=True, exist_ok=True)
        mesh.parent.mkdir(parents=True, exist_ok=True)
        splat.write_text("ply\n")
        mesh.write_text("ply\n")
        return {"ok": True, "splat_path": str(splat), "mesh_path": str(mesh),
                "backend": "brush"}

    db.run_openmvs = run_openmvs
    db.run_brush = run_brush

    # mesh_cleanup
    mc = _mod("engine_oracle.vision.mesh_cleanup")

    def cleanup_mesh(mesh_path, out_path, **kw):
        calls["cleanup"].append({"mesh_path": mesh_path, "out_path": out_path, **kw})
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("cleaned\n")
        return {"ok": True, "mesh_path": out_path, "n_vertices": 10, "n_triangles": 12}

    mc.cleanup_mesh = cleanup_mesh

    # segment_fit
    if "segment_fit" in fail_imports:
        class _BoomFit:
            def __getattr__(self, _n):
                raise ImportError("open3d missing")
        monkeypatch.setitem(sys.modules, "engine_oracle.vision.segment_fit", _BoomFit())  # type: ignore
    else:
        sf = _mod("engine_oracle.vision.segment_fit")

        def segment_fit(mesh_path=None, *, session_id="", **kw):
            calls["fit"].append({"mesh_path": mesh_path, "session_id": session_id, **kw})
            if not fit_ok:
                return {"ok": False, "reason": "backend_not_provisioned",
                        "hint": "pip install open3d"}
            return {
                "schema": "aicad.reveng_fits/1",
                "ok": True,
                "session_id": session_id,
                "mesh_path": mesh_path or "",
                "fits": [{
                    "kind": "cylinder", "confidence": 0.9,
                    "params_mm": {"radius": 8.0, "height": 12.0,
                                  "axis": [0, 0, 1], "origin": [0, 0, 0]},
                    "source": "ransac", "face_ids": ["f1"],
                }],
                "sections": [],
            }

        sf.segment_fit = segment_fit

    # cad_recode (strategy=ml)
    if "cad_recode" in fail_imports:
        class _BoomML:
            def __getattr__(self, _n):
                raise ImportError("cad_recode missing")
        monkeypatch.setitem(sys.modules, "engine_oracle.vision.cad_recode", _BoomML())  # type: ignore
    else:
        cr = _mod("engine_oracle.vision.cad_recode")

        def build_parametric(mesh_path, session_dir, scale_transform=None, device=None):
            calls["ml"].append({
                "mesh_path": mesh_path,
                "session_dir": session_dir,
                "scale_transform": scale_transform,
                "device": device,
            })
            if not ml_ok:
                return {"ok": False, "error": "not provisioned: cad-recode weights",
                        "cadquery_src": None, "step_path": None}
            step = Path(session_dir) / "cad_recode.step"
            step.write_text("ISO-10303-21;\n")
            return {
                "ok": True,
                "cadquery_src": "import cadquery as cq\nresult = cq.Workplane('XY').box(1,1,1)\n",
                "step_path": str(step),
                "error": None,
                "provenance": {"method": "ml_cad_recode",
                               "model": "filapro/cad-recode-v1.5"},
            }

        cr.build_parametric = build_parametric

    # reverse_engineer helper
    def _fake_re(source, *, name=None, hints=None):
        calls["re"].append({"source": source, "name": name, "hints": hints})
        if not re_ok:
            return {"ok": False, "error": "reverse_engineer failed"}
        return {
            "ok": True,
            "doc_id": "doc_rebuilt_1",
            "ref_doc_id": "doc_ref_1",
            "match_pct": 92.5,
            "features": [],
            "rejected": [],
            "candidates": [],
            "notes": [],
        }

    monkeypatch.setattr(rc, "_call_reverse_engineer", _fake_re)

    # reveng_status capabilities (so open paths aren't pre-gated elsewhere)
    _patch_reveng_status(monkeypatch, lambda: {
        "ok": True,
        "capabilities": {
            "sparse": True, "dense": True, "splat": True,
            "single_photo": True, "webcam": False, "gpu": False,
        },
        "provisioning": {},
        "banners": [],
        "missing": [],
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
        assert calls["sparse"], "run_sparse must be called"
        assert calls["sparse"][0]["image_dir"].endswith("images")
        assert calls["openmvs"], "run_openmvs must be called when OPENMVS_BIN set"
        assert calls["cleanup"], "cleanup_mesh must be called"
        # ffwd not needed when registration strong (>=60%)
        assert not calls["ffwd"]

        s2 = rt._load_session(sid)
        assert s2["artifacts"]["mesh_path"]
        assert s2["artifacts"]["sparse_dir"]
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

    def test_sparse_import_failure_degrades_honestly(self, session_env, monkeypatch):
        sid, _root, _mesh = session_env
        s = rt._load_session(sid)
        s["artifacts"]["mesh_path"] = None
        rt._save_session(s)

        # Make colmap import fail AND ffwd fail
        class _Boom:
            def __getattr__(self, _n):
                raise ImportError("no colmap")

        monkeypatch.setitem(sys.modules, "engine_oracle.vision.colmap_pipeline", _Boom())
        monkeypatch.setitem(sys.modules, "engine_oracle.vision.ffwd_sfm", _Boom())
        _patch_reveng_status(monkeypatch, lambda: {
            "ok": True,
            "capabilities": {"sparse": False, "dense": False},
            "provisioning": {}, "banners": [], "missing": [],
        })

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("reconstruct", {"node_id": "n_reconstruct"})
        assert res.success is False
        detail = (res.result or {})
        reason = detail.get("detail") or detail.get("reason") or ""
        # Accept either explicit reason on result or stop_reason path
        blob = json.dumps(detail) + reason
        assert "backend_not_provisioned" in blob or "unavailable" in blob or "import" in blob.lower() or res.stop_reason == "failed"


class TestFitNode:
    def test_segment_fit_persists_fits(self, session_env, monkeypatch):
        sid, root, mesh = session_env
        calls = _install_backend(monkeypatch)

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("fit", {"node_id": "n_fit"})
        assert res.success is True, res.result
        assert calls["fit"], "segment_fit must be called"
        assert calls["fit"][0]["mesh_path"] == str(mesh)
        assert calls["fit"][0]["session_id"] == sid
        fits_path = root / "fits.json"
        assert fits_path.is_file()
        doc = json.loads(fits_path.read_text())
        assert doc["schema"] == "aicad.reveng_fits/1"
        assert doc["fits"]

    def test_fit_import_failure_degrades(self, session_env, monkeypatch):
        sid, _root, _mesh = session_env
        _install_backend(monkeypatch, fail_imports={"segment_fit"})

        fn = rt._make_pipeline_agent_fn(sid)
        res = fn("fit", {"node_id": "n_fit"})
        assert res.success is False
        blob = json.dumps(res.result or {})
        assert "segment_fit" in blob or "backend_not_provisioned" in blob


class TestParametricNode:
    def test_deterministic_calls_reverse_engineer_with_hints(
        self, session_env, monkeypatch,
    ):
        sid, root, mesh = session_env
        calls = _install_backend(monkeypatch)
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
        assert calls["re"], "reverse_engineer must be called"
        assert calls["re"][0]["source"] == str(mesh)
        assert calls["re"][0]["hints"] is not None
        assert calls["re"][0]["hints"]["schema"] == "aicad.reveng_fits/1"

        s2 = rt._load_session(sid)
        assert s2["artifacts"]["doc_id"] == "doc_rebuilt_1"

    def test_ml_strategy_calls_build_parametric(self, session_env, monkeypatch):
        sid, root, mesh = session_env
        calls = _install_backend(monkeypatch, ml_ok=True)

        fn = rt._make_pipeline_agent_fn(sid, strategy="ml")
        res = fn("parametric", {"node_id": "n_parametric"})
        assert res.success is True, res.result
        assert calls["ml"], "build_parametric must be called for strategy=ml"
        assert calls["ml"][0]["mesh_path"] == str(mesh)
        assert calls["ml"][0]["session_dir"] == str(root)
        # scale_transform is None when session has no scale
        assert calls["ml"][0]["scale_transform"] is None

    def test_ml_import_failure_degrades(self, session_env, monkeypatch):
        sid, _root, _mesh = session_env
        _install_backend(monkeypatch, fail_imports={"cad_recode"})

        fn = rt._make_pipeline_agent_fn(sid, strategy="ml")
        res = fn("parametric", {"node_id": "n_parametric"})
        assert res.success is False
        blob = json.dumps(res.result or {})
        assert "cad_recode" in blob or "backend_not_provisioned" in blob


class TestMcpToolUnstubs:
    def test_fit_primitives_tool_calls_segment_fit(self, session_env, monkeypatch):
        sid, _root, mesh = session_env
        calls = _install_backend(monkeypatch)

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
        raw = mcp._tools["reveng_fit_primitives"](sid)
        out = json.loads(raw)
        assert out.get("ok") is True, out
        assert calls["fit"]
        assert calls["fit"][0]["mesh_path"] == str(mesh)

    def test_single_photo_tool_tags_honesty(self, session_env, monkeypatch):
        sid, root, _mesh = session_env
        calls = {"sp": []}

        sp = types.ModuleType("engine_oracle.vision.single_photo")

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

        sp.reconstruct_single_photo = reconstruct_single_photo
        monkeypatch.setitem(sys.modules, "engine_oracle.vision.single_photo", sp)
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
