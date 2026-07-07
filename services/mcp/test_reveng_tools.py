"""Tests for services/mcp/reveng_tools.py — contracts/reveng.md §8.

Round-trips every live tool against the contract shapes.  Long-job tools
(reconstruct / build_parametric / single_photo / fit_primitives) are verified
to return the honest ``backend_not_provisioned`` envelope.

Imports are standalone (no libaicad.so, no MCP server running): we stub
FastMCP and core_bridge the same way test_mcp_freshness.py does, then call
register_reveng_tools() directly with the stub MCP object and invoke the
registered functions by name.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Lightweight stubs (mirrors test_mcp_freshness.py pattern)
# ---------------------------------------------------------------------------

def _install_stubs():
    # core_bridge stub
    cb_mod = types.ModuleType("mcp_server.core_bridge")
    cb_mod.available = lambda: False
    cb_mod.call = lambda method, params=None: {}
    sys.modules.setdefault("mcp_server.core_bridge", cb_mod)
    sys.modules.setdefault("core_bridge", cb_mod)

    # mcp package stub
    if "mcp" not in sys.modules:
        mcp_pkg = types.ModuleType("mcp")
        server_mod = types.ModuleType("mcp.server")
        fastmcp_mod = types.ModuleType("mcp.server.fastmcp")

        class _FakeFastMCP:
            def __init__(self, name="", **kw):
                self._tools: dict = {}

            def tool(self):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

        fastmcp_mod.FastMCP = _FakeFastMCP
        sys.modules["mcp"] = mcp_pkg
        sys.modules["mcp.server"] = server_mod
        sys.modules["mcp.server.fastmcp"] = fastmcp_mod

    # cad_tools stub — only _dumps/_err needed
    if "mcp_server.cad_tools" not in sys.modules:
        ct_mod = types.ModuleType("mcp_server.cad_tools")

        def _dumps(obj):
            return json.dumps(obj)

        def _err(e):
            return json.dumps({"ok": False, "error": str(e)})

        ct_mod._dumps = _dumps
        ct_mod._err = _err
        sys.modules["mcp_server.cad_tools"] = ct_mod


_install_stubs()


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
# reveng_provision and frames may not be importable in CI without heavy deps.
# Patch them at module import time with lightweight stubs.

def _stub_reveng_status():
    return {
        "ok": True,
        "capabilities": {
            "sparse": False, "dense": False, "splat": False,
            "single_photo": False, "webcam": False, "gpu": False,
        },
        "provisioning": {k: False for k in
                         ("colmap", "openmvs", "brush", "triposr", "vggt", "docling")},
        "banners": ["stub"],
        "missing": ["colmap", "openmvs", "brush", "triposr", "vggt", "docling"],
    }


def _stub_extract_frames_safe(video_path, out_dir, **kw):
    # Pretend it extracted 5 frames (no real OpenCV needed)
    return {
        "ok": True,
        "frames": [
            {"path": f"{out_dir}/frame_{i:04d}.jpg",
             "frame_index": i * 10, "time_s": i * 0.5, "sharpness": 200.0}
            for i in range(5)
        ],
        "kept": 5,
        "sampled": 20,
        "rejected_blurry": 15,
        "total_frames": 100,
        "blur_threshold": 100.0,
    }


def _stub_ingest_pdf(path, out_dir):
    return {
        "ok": True,
        "tables": [{"header": ["Dim", "Value"], "rows": [["A", "10mm"]]}],
        "crops": [f"{out_dir}/crop_0.png"],
    }


# Patch before importing the module so the fallback stubs inside reveng_tools
# are bypassed (the real modules may not exist in this environment).
with (
    patch.dict("sys.modules", {
        "engine_oracle": types.ModuleType("engine_oracle"),
        "engine_oracle.vision": types.ModuleType("engine_oracle.vision"),
        "engine_oracle.vision.reveng_provision": types.ModuleType("engine_oracle.vision.reveng_provision"),
        "engine_oracle.vision.frames": types.ModuleType("engine_oracle.vision.frames"),
        "engine_oracle.vision.ingest": types.ModuleType("engine_oracle.vision.ingest"),
    }),
):
    sys.modules["engine_oracle.vision.reveng_provision"].reveng_status = _stub_reveng_status
    sys.modules["engine_oracle.vision.frames"].extract_frames_safe = _stub_extract_frames_safe
    sys.modules["engine_oracle.vision.ingest"].ingest_pdf = _stub_ingest_pdf

    from services.mcp.reveng_tools import register_reveng_tools  # noqa: E402


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _build_tools(extra_env: dict | None = None) -> dict:
    """Register tools and return the name→function dict."""
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    register_reveng_tools(mcp)
    return mcp._tools


def _call(tools: dict, tool_name: str, **kwargs) -> dict:
    fn = tools[tool_name]
    raw = fn(**kwargs)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRevengToolsRegistration(unittest.TestCase):

    def setUp(self):
        self.tools = _build_tools()

    def test_all_expected_tools_registered(self):
        expected = {
            "list_reveng_sessions",
            "get_reveng_session",
            "get_reveng_coverage",
            "get_reveng_deviation",
            "describe_reveng_result",
            "reveng_status",
            "reveng_start_session",
            "reveng_add_images",
            "reveng_add_video",
            "reveng_ingest_datasheet",
            "reveng_set_scale",
            "reveng_reconstruct",
            "reveng_single_photo",
            "reveng_fit_primitives",
            "reveng_build_parametric",
            "reveng_attach_result",
            "reveng_print_scale_mat",
            "import_scan_mesh",
            "export_reveng_bundle",
        }
        registered = set(self.tools.keys())
        missing = expected - registered
        self.assertFalse(missing, f"Tools not registered: {missing}")


class TestRevengSessionLifecycle(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AICAD_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AICAD_REVENG_DIR", None)

    def test_start_session_returns_session_id(self):
        r = _call(self.tools, "reveng_start_session", name="Test bracket")
        self.assertTrue(r["ok"])
        self.assertIn("session_id", r)
        self.assertTrue(r["session_id"].startswith("re_"))
        self.assertEqual(r["session"]["name"], "Test bracket")
        self.assertEqual(r["session"]["schema"], "aicad.reveng_session/1")

    def test_start_session_with_part_ref(self):
        r = _call(self.tools, "reveng_start_session",
                  name="M8 bracket", part_ref="catalog:grabcad:bracket-123")
        self.assertTrue(r["ok"])
        self.assertEqual(r["session"]["part_ref"], "catalog:grabcad:bracket-123")

    def test_list_sessions_empty(self):
        r = _call(self.tools, "list_reveng_sessions")
        self.assertTrue(r["ok"])
        self.assertEqual(r["sessions"], [])

    def test_list_sessions_after_create(self):
        _call(self.tools, "reveng_start_session", name="S1")
        _call(self.tools, "reveng_start_session", name="S2")
        r = _call(self.tools, "list_reveng_sessions")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["sessions"]), 2)
        names = {s["name"] for s in r["sessions"]}
        self.assertEqual(names, {"S1", "S2"})

    def test_get_session_round_trip(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "get_reveng_session", session_id=sid)
        self.assertTrue(r["ok"])
        self.assertEqual(r["session"]["session_id"], sid)
        self.assertEqual(r["session"]["schema"], "aicad.reveng_session/1")
        # Required fields per contract §2
        for field in ("status", "stages", "scale", "artifacts", "honesty", "provenance"):
            self.assertIn(field, r["session"])

    def test_get_session_not_found(self):
        r = _call(self.tools, "get_reveng_session", session_id="re_deadbeef")
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)

    def test_coverage_new_session(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "get_reveng_coverage", session_id=sid)
        self.assertTrue(r["ok"])
        cov = r["coverage"]
        self.assertEqual(cov["plan"], "11_shot")
        self.assertIsInstance(cov["captured"], list)
        self.assertIsInstance(cov["missing"], list)
        self.assertIn(cov["quality"], ("good", "partial", "insufficient"))
        # No images yet → all missing
        self.assertEqual(len(cov["missing"]), 11)

    def test_add_images_from_paths(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        # Create a couple of fake image files
        img_dir = tempfile.mkdtemp()
        try:
            for i in range(3):
                p = Path(img_dir) / f"img_{i:03d}.jpg"
                p.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)  # minimal JPEG header
            paths = [str(Path(img_dir) / f"img_{i:03d}.jpg") for i in range(3)]
            r = _call(self.tools, "reveng_add_images", session_id=sid, paths=paths)
            self.assertTrue(r["ok"])
            self.assertEqual(r["added"], 3)
            self.assertEqual(r["session"]["artifacts"]["images"], 3)
            self.assertEqual(r["session"]["stages"]["capture"], "done")
        finally:
            import shutil
            shutil.rmtree(img_dir, ignore_errors=True)

    def test_add_images_dir(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        img_dir = tempfile.mkdtemp()
        try:
            for i in range(2):
                (Path(img_dir) / f"shot_{i}.png").write_bytes(b"\x89PNG\r\n" + b"\x00" * 10)
            r = _call(self.tools, "reveng_add_images", session_id=sid, dir=img_dir)
            self.assertTrue(r["ok"])
            self.assertEqual(r["added"], 2)
        finally:
            import shutil
            shutil.rmtree(img_dir, ignore_errors=True)

    def test_add_video_delegates_to_extract_frames(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        # The stub extract_frames_safe doesn't need a real video file
        # but reveng_add_video will call it with whatever path we give
        r = _call(self.tools, "reveng_add_video",
                  session_id=sid, path="/fake/orbit.mp4", max_frames=60)
        self.assertTrue(r["ok"])
        self.assertIn("run_id", r)
        self.assertTrue(r["run_id"].startswith("run_"))
        # 5 frames added by stub
        self.assertEqual(r["session"]["artifacts"]["images"], 5)
        self.assertEqual(r["session"]["stages"]["capture"], "done")
        # provenance entry recorded
        prov_steps = [p["step"] for p in r["session"]["provenance"]]
        self.assertIn("add_video", prov_steps)

    def test_ingest_datasheet(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_ingest_datasheet",
                  session_id=sid, path="/fake/datasheet.pdf")
        self.assertTrue(r["ok"])
        self.assertIsInstance(r["tables"], list)
        self.assertIsInstance(r["crops"], list)
        self.assertIn("session", r)

    def test_set_scale_two_point(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid,
                  method="two_point",
                  params={"image": "img_001.jpg",
                          "p1": [100, 200], "p2": [300, 200],
                          "distance_mm": 40.0})
        self.assertTrue(r["ok"])
        self.assertEqual(r["scale"]["method"], "two_point")
        self.assertEqual(r["scale"]["mm_per_unit"], 40.0)
        self.assertFalse(r["scale"]["confirmed"])  # gate not confirmed yet

    def test_set_scale_invalid_method(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid, method="ruler", params={})
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)
        self.assertIn("hint", r)

    def test_set_scale_aruco_mat(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid,
                  method="aruco_mat",
                  params={"board": "A4"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["scale"]["method"], "aruco_mat")

    def test_set_scale_known_dim(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid,
                  method="known_dim",
                  params={"feature": "hex_width", "value_mm": 13.0, "source": "datasheet"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["scale"]["mm_per_unit"], 13.0)

    def test_attach_result(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_attach_result",
                  session_id=sid, part_ref="catalog:grabcad:bracket-123")
        self.assertTrue(r["ok"])
        self.assertEqual(r["part_ref"], "catalog:grabcad:bracket-123")
        self.assertIn("session", r)
        self.assertEqual(r["session"]["part_ref"], "catalog:grabcad:bracket-123")

    def test_import_scan_mesh(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        # Create a fake .ply file
        ply_dir = tempfile.mkdtemp()
        try:
            ply = Path(ply_dir) / "cleaned.ply"
            ply.write_bytes(b"ply\nformat ascii 1.0\n")
            r = _call(self.tools, "import_scan_mesh",
                      session_id=sid, path=str(ply), scale_mm=1.0)
            self.assertTrue(r["ok"])
            self.assertIn("mesh_path", r)
            self.assertEqual(r["session"]["scale"]["mm_per_unit"], 1.0)
            prov_steps = [p["step"] for p in r["session"]["provenance"]]
            self.assertIn("import_scan_mesh", prov_steps)
        finally:
            import shutil
            shutil.rmtree(ply_dir, ignore_errors=True)

    def test_export_reveng_bundle(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        dest = str(Path(self.tmp) / "bundle.zip")
        r = _call(self.tools, "export_reveng_bundle", session_id=sid, dest=dest)
        self.assertTrue(r["ok"])
        self.assertTrue(Path(r["path"]).exists())
        import zipfile
        with zipfile.ZipFile(dest) as zf:
            names = zf.namelist()
        self.assertIn("session.json", names)


class TestRevengStatusTool(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AICAD_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AICAD_REVENG_DIR", None)

    def test_reveng_status_shape(self):
        """reveng_status() returns the contract §8.1 envelope."""
        r = _call(self.tools, "reveng_status")
        self.assertTrue(r["ok"])
        self.assertIn("capabilities", r)
        self.assertIn("provisioning", r)
        self.assertIn("banners", r)
        caps = r["capabilities"]
        for key in ("sparse", "dense", "splat", "single_photo", "webcam", "gpu"):
            self.assertIn(key, caps)
        prov = r["provisioning"]
        for key in ("colmap", "openmvs", "brush", "triposr", "vggt", "docling"):
            self.assertIn(key, prov)

    def test_reveng_status_booleans(self):
        r = _call(self.tools, "reveng_status")
        for v in r["capabilities"].values():
            self.assertIsInstance(v, bool)
        for v in r["provisioning"].values():
            self.assertIsInstance(v, bool)


class TestRevengLongJobStubs(unittest.TestCase):
    """Long-job tools must return the honest backend_not_provisioned envelope."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AICAD_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        self.sid = create["session_id"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AICAD_REVENG_DIR", None)

    def _assert_not_provisioned(self, result: dict):
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "backend_not_provisioned")
        self.assertIn("hint", result)

    def test_reveng_reconstruct_stub(self):
        r = _call(self.tools, "reveng_reconstruct",
                  session_id=self.sid, quality="normal")
        self._assert_not_provisioned(r)

    def test_reveng_single_photo_stub(self):
        r = _call(self.tools, "reveng_single_photo",
                  session_id=self.sid, tier="triposr")
        self._assert_not_provisioned(r)

    def test_reveng_fit_primitives_stub(self):
        r = _call(self.tools, "reveng_fit_primitives", session_id=self.sid)
        self._assert_not_provisioned(r)

    def test_reveng_build_parametric_stub(self):
        r = _call(self.tools, "reveng_build_parametric", session_id=self.sid)
        self._assert_not_provisioned(r)

    def test_reveng_build_parametric_refuses_approximate(self):
        """Refuses approximate sessions without override."""
        # Manually mark the session as approximate
        import json as _j
        from pathlib import Path as _P
        p = _P(self.tmp) / self.sid / "session.json"
        s = _j.loads(p.read_text())
        s["honesty"]["approximate"] = True
        p.write_text(_j.dumps(s))

        r = _call(self.tools, "reveng_build_parametric", session_id=self.sid)
        self.assertFalse(r["ok"])
        self.assertIn("approximate", r["reason"])
        self.assertIn("hint", r)

    def test_reveng_build_parametric_override_approximate(self):
        """With override, falls through to the backend_not_provisioned stub."""
        import json as _j
        from pathlib import Path as _P
        p = _P(self.tmp) / self.sid / "session.json"
        s = _j.loads(p.read_text())
        s["honesty"]["approximate"] = True
        p.write_text(_j.dumps(s))

        r = _call(self.tools, "reveng_build_parametric",
                  session_id=self.sid,
                  strategy="override_approximate")
        self._assert_not_provisioned(r)


class TestRevengDeviationAndResult(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AICAD_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        self.sid = create["session_id"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AICAD_REVENG_DIR", None)

    def test_get_deviation_no_model_yet(self):
        r = _call(self.tools, "get_reveng_deviation", session_id=self.sid)
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)

    def test_describe_result_no_model_yet(self):
        r = _call(self.tools, "describe_reveng_result", session_id=self.sid)
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)


class TestRevengPrintScaleMat(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["AICAD_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("AICAD_REVENG_DIR", None)

    def test_invalid_size(self):
        r = _call(self.tools, "reveng_print_scale_mat", size="A3")
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)

    def test_not_provisioned_without_module(self):
        # scale_mat module not installed in test env → backend_not_provisioned
        r = _call(self.tools, "reveng_print_scale_mat", size="A4")
        # either ok (if scale_mat happened to import) or not_provisioned — both legal
        self.assertIn("ok", r)
        if not r["ok"]:
            self.assertIn(r["reason"], ("backend_not_provisioned",))


if __name__ == "__main__":
    unittest.main()
