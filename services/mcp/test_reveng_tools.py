"""Tests for services/mcp/reveng_tools.py — contracts/reveng.md §8.

Session lifecycle, capture retention, scale solving, and honest backend failures.
Use a local registration collector with the real MCP/runtime modules; no package
or import-cache mutation is needed and no server or engine process is started.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from services.mcp import reveng_build_tools as rb
from services.mcp import reveng_common as rc
from services.mcp import reveng_session_tools as rs
from services.mcp.reveng_tools import register_reveng_tools


class _ToolCollector:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def register(fn):
            self._tools[fn.__name__] = fn
            return fn
        return register


def _unavailable_status():
    return {
        "ok": True,
        "capabilities": {
            "sparse": False, "dense": False, "splat": False,
            "single_photo": False, "webcam": False, "gpu": False,
        },
        "provisioning": {},
        "banners": [],
        "missing": [],
    }


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _build_tools() -> dict:
    """Register tools without replacing the MCP package."""
    mcp = _ToolCollector()
    register_reveng_tools(mcp)
    return mcp._tools


def _call(tools: dict, tool_name: str, **kwargs) -> dict:
    fn = tools[tool_name]
    raw = fn(**kwargs)
    return json.loads(raw)


def _write_real_image(path: Path, value: int):
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise unittest.SkipTest("OpenCV and numpy required for image ingestion")
    image = np.full((48, 64, 3), value, dtype=np.uint8)
    image[::2, ::2] = 255
    assert cv2.imwrite(str(path), image)


def _write_real_video(path: Path, value: int):
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise unittest.SkipTest("OpenCV and numpy required for video ingestion")
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"FFV1"), 10, (64, 48))
    assert writer.isOpened()
    for idx in range(6):
        image = np.full((48, 64, 3), value + idx * 12, dtype=np.uint8)
        image[::2, ::2] = 255
        writer.write(image)
    writer.release()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class _RevengTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for module in (rc, rb, rs):
            patcher = patch.object(module, "_reveng_status", _unavailable_status)
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        environment = patch.dict(os.environ)
        environment.start()
        cls.addClassCleanup(environment.stop)


class TestRevengSessionLifecycle(_RevengTestBase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CHUDWARE_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CHUDWARE_REVENG_DIR", None)

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
        # Distinct valid images are counted only after decode and publication.
        img_dir = tempfile.mkdtemp()
        try:
            for i in range(3):
                p = Path(img_dir) / f"img_{i:03d}.jpg"
                _write_real_image(p, i * 30)
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
                _write_real_image(Path(img_dir) / f"shot_{i}.png", i * 30)
            r = _call(self.tools, "reveng_add_images", session_id=sid, dir=img_dir)
            self.assertTrue(r["ok"])
            self.assertEqual(r["added"], 2)
        finally:
            import shutil
            shutil.rmtree(img_dir, ignore_errors=True)

    def test_two_videos_and_still_survive_repeat_import_and_reopen(self):
        from services.vision.frames import extract_frames_safe
        import services.mcp.reveng_session_tools as tools

        sid = _call(self.tools, "reveng_start_session", name="Orbit")["session_id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, still = root / "first.avi", root / "second.avi", root / "live.png"
            _write_real_video(first, 0)
            _write_real_video(second, 70)
            _write_real_image(still, 25)
            with patch.object(tools, "_extract_frames_safe", extract_frames_safe):
                a = _call(self.tools, "reveng_add_video", session_id=sid, path=str(first), max_frames=3)
                b = _call(self.tools, "reveng_add_video", session_id=sid, path=str(second), max_frames=3)
                capture = _call(self.tools, "reveng_add_images", session_id=sid, paths=[str(still)])
                repeat = _call(self.tools, "reveng_add_video", session_id=sid, path=str(first), max_frames=3)
            self.assertTrue(a["ok"], a)
            self.assertTrue(b["ok"], b)
            self.assertTrue(capture["ok"], capture)
            self.assertTrue(repeat["ok"], repeat)
            self.assertEqual(repeat["added"], 0)
            self.assertEqual(repeat["session"]["artifacts"]["images"], 7)
            self.assertEqual(len([p for p in repeat["session"]["provenance"]
                                  if p["step"] == "add_video"]), 2)
            reopened = _call(self.tools, "get_reveng_session", session_id=sid)
            gallery = reopened["source_images"]
            self.assertEqual(len(gallery), 7)
            self.assertEqual(len({row["path"] for row in gallery}), 7)
            self.assertTrue(all(Path(row["path"]).is_file() for row in gallery))
            self.assertTrue(all(row["blur_score"] is not None for row in gallery))
            self.assertEqual({row["source_path"] for row in gallery if row["kind"] == "video"},
                             {str(first), str(second)})

    def test_same_named_stills_do_not_overwrite_and_repeat_is_idempotent(self):
        sid = _call(self.tools, "reveng_start_session", name="Stills")["session_id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "a", root / "b"
            first.mkdir()
            second.mkdir()
            _write_real_image(first / "capture.png", 10)
            _write_real_image(second / "capture.png", 80)
            sources = [str(first / "capture.png"), str(second / "capture.png")]
            imported = _call(self.tools, "reveng_add_images", session_id=sid, paths=sources)
            repeated = _call(self.tools, "reveng_add_images", session_id=sid, paths=sources)
            self.assertTrue(imported["ok"], imported)
            self.assertTrue(repeated["ok"], repeated)
            self.assertEqual(imported["added"], 2)
            self.assertEqual(repeated["added"], 0)
            self.assertEqual(repeated["session"]["artifacts"]["images"], 2)
            reopened = _call(self.tools, "get_reveng_session", session_id=sid)
            self.assertEqual({Path(row["path"]).read_bytes() for row in reopened["source_images"]},
                             {Path(source).read_bytes() for source in sources})

    def test_bad_still_is_refused_without_losing_valid_sources(self):
        sid = _call(self.tools, "reveng_start_session", name="Decode")["session_id"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good, bad = root / "good.png", root / "bad.png"
            _write_real_image(good, 10)
            bad.write_bytes(b"not an image")
            result = _call(self.tools, "reveng_add_images", session_id=sid,
                           paths=[str(good), str(bad), str(root / "missing.png")])
            self.assertFalse(result["ok"])
            self.assertEqual(result["added"], 1)
            self.assertEqual(result["session"]["artifacts"]["images"], 1)
            self.assertEqual({row["path"] for row in result["failed"]},
                             {str(bad), str(root / "missing.png")})

    def test_ingest_datasheet(self):
        import os
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        # A fake path must honestly fail (the tool no longer no-ops to ok=True).
        bad = _call(self.tools, "reveng_ingest_datasheet",
                    session_id=sid, path="/fake/datasheet.pdf")
        self.assertFalse(bad["ok"])
        # Real bearing datasheet fixture (RE-15): pdfplumber extracts its table.
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        pdf = os.path.join(repo, "corpora", "reveng", "bearing_spec.pdf")
        r = _call(self.tools, "reveng_ingest_datasheet", session_id=sid, path=pdf)
        if not r.get("ok"):
            # No PDF backend (docling/pdfplumber) provisioned — honest degradation.
            self.assertEqual(r.get("reason"), "backend_not_provisioned")
            return
        self.assertIsInstance(r["tables"], list)
        self.assertIsInstance(r["crops"], list)
        self.assertIn("session", r)

    def _write_sparse_model(self, sid):
        """Write a minimal COLMAP text model whose geometry has a known metric answer.

        One PINHOLE camera (f=500, c=320/240) at the origin looking down +Z. Two sparse
        points 2.0 reconstruction-units apart project to pixels (270, 240) and (370, 240),
        so clicking those two with a real separation of 50 mm must solve mm_per_unit = 25.
        """
        sparse = Path(self.tmp) / sid / "sparse" / "0"
        sparse.mkdir(parents=True, exist_ok=True)
        (sparse / "cameras.txt").write_text(
            "# Camera list\n1 PINHOLE 640 480 500 500 320 240\n"
        )
        (sparse / "images.txt").write_text(
            "# Image list\n1 1 0 0 0 0 0 0 1 img_001.jpg\n100 200 -1\n"
        )
        (sparse / "points3D.txt").write_text(
            "# 3D point list\n"
            "1 -1.0 0.0 10.0 255 0 0 0.5 1 0\n"
            "2 1.0 0.0 10.0 0 255 0 0.5 1 1\n"
            "3 5.0 5.0 10.0 0 0 255 0.5 1 2\n"
        )
        sess_json = Path(self.tmp) / sid / "session.json"
        doc = json.loads(sess_json.read_text())
        doc["artifacts"]["sparse_dir"] = str(sparse)
        sess_json.write_text(json.dumps(doc, indent=2))
        return sparse

    def test_set_scale_two_point_needs_sparse(self):
        """Without a reconstruction there is nothing to scale — refuse, never echo the input."""
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid,
                  method="two_point",
                  params={"image": "img_001.jpg",
                          "p1": [100, 200], "p2": [300, 200],
                          "distance_mm": 40.0})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "scale_needs_sparse")
        self.assertIn("hint", r)
        # The old bug: distance_mm copied straight into mm_per_unit.
        session = _call(self.tools, "get_reveng_session", session_id=sid)["session"]
        self.assertIsNone(session["scale"]["mm_per_unit"])

    def test_set_scale_two_point_solves(self):
        """Positive path: the tool really runs solve_scale_two_point over the sparse model."""
        try:
            import numpy  # noqa: F401
        except ImportError:  # pragma: no cover - numpy rides with opencv
            self.skipTest("numpy not installed")
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        self._write_sparse_model(sid)
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid,
                  method="two_point",
                  params={"image": "img_001.jpg",
                          "p1": [270, 240], "p2": [370, 240],
                          "distance_mm": 50.0})
        self.assertTrue(r["ok"], r)
        scale = r["scale"]
        self.assertEqual(scale["method"], "two_point")
        # 50 mm over a 2.0-unit span — solved, not echoed (the echo would be 50.0).
        self.assertAlmostEqual(scale["mm_per_unit"], 25.0, places=6)
        self.assertAlmostEqual(scale["residual_pct"], 0.0, places=6)
        self.assertFalse(scale["confirmed"])  # gate not confirmed yet
        self.assertEqual(scale["detail"]["image"], "img_001.jpg")
        self.assertAlmostEqual(scale["detail"]["dist_units"], 2.0, places=6)
        prov = r["session"]["provenance"][-1]
        self.assertEqual(prov["step"], "set_scale")
        self.assertEqual(prov["source"], "photo_fiducial")

    def test_set_scale_invalid_method(self):
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid, method="ruler", params={})
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)
        self.assertIn("hint", r)

    def test_set_scale_aruco_mat_needs_sparse(self):
        """aruco_mat triangulates the marker against the sparse model — no model, no scale."""
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        sid = create["session_id"]
        r = _call(self.tools, "reveng_set_scale",
                  session_id=sid,
                  method="aruco_mat",
                  params={"board": "A4"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "scale_needs_sparse")
        self.assertIn("hint", r)
        session = _call(self.tools, "get_reveng_session", session_id=sid)["session"]
        self.assertIsNone(session["scale"]["mm_per_unit"])

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
            gaussian = _call(self.tools, "reveng_splat_status", session_id=sid)
            self.assertFalse(gaussian["ok"])
            self.assertEqual(gaussian["reason"], "no_splat_job")
            reopened = _call(self.tools, "get_reveng_session", session_id=sid)
            self.assertEqual(reopened["session"]["artifacts"]["mesh_path"], r["mesh_path"])
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




class TestRevengBackendFailures(_RevengTestBase):
    """Unavailable backends must refuse rather than fabricate successful jobs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CHUDWARE_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        self.sid = create["session_id"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CHUDWARE_REVENG_DIR", None)


    def _assert_not_provisioned(self, result: dict):
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "backend_not_provisioned")
        self.assertIn("hint", result)

    def test_reveng_reconstruct_stub(self):
        r = _call(self.tools, "reveng_reconstruct",
                  session_id=self.sid, quality="normal")
        self._assert_not_provisioned(r)

    def test_reveng_single_photo_stub(self):
        # The input guard fires first on an image-less session (honest no_images);
        # with a photo seeded the backend guard must answer backend_not_provisioned.
        r0 = _call(self.tools, "reveng_single_photo",
                   session_id=self.sid, tier="triposr")
        self.assertFalse(r0["ok"])
        self.assertEqual(r0["reason"], "no_images")
        imgdir = Path(self.tmp) / self.sid / "images"
        imgdir.mkdir(parents=True, exist_ok=True)
        _write_real_image(imgdir / "p.png", 35)
        with patch("services.vision.single_photo.reconstruct_single_photo",
                   return_value={"ok": False, "reason": "backend_not_provisioned",
                                 "hint": "single-photo backend unavailable"}):
            r = _call(self.tools, "reveng_single_photo",
                      session_id=self.sid, tier="triposr")
        self._assert_not_provisioned(r)

    def test_reveng_fit_primitives_stub(self):
        # Same shape: no mesh yet -> honest no_mesh; mesh seeded -> backend guard.
        r0 = _call(self.tools, "reveng_fit_primitives", session_id=self.sid)
        self.assertFalse(r0["ok"])
        self.assertEqual(r0["reason"], "no_mesh")
        sess_json = Path(self.tmp) / self.sid / "session.json"
        sess = json.loads(sess_json.read_text())
        mesh = Path(self.tmp) / self.sid / "mesh.ply"
        mesh.write_text("ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")
        sess.setdefault("artifacts", {})["mesh_path"] = str(mesh)
        sess_json.write_text(json.dumps(sess))
        with patch("services.vision.segment_fit.segment_fit",
                   return_value={"ok": False, "reason": "backend_not_provisioned",
                                 "hint": "mesh fitting backend unavailable"}):
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


class TestRevengDeviationAndResult(_RevengTestBase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CHUDWARE_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()
        create = _call(self.tools, "reveng_start_session", name="Bracket")
        self.sid = create["session_id"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CHUDWARE_REVENG_DIR", None)

    def test_get_deviation_no_model_yet(self):
        r = _call(self.tools, "get_reveng_deviation", session_id=self.sid)
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)

    def test_describe_result_no_model_yet(self):
        r = _call(self.tools, "describe_reveng_result", session_id=self.sid)
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)


class TestRevengPrintScaleMat(_RevengTestBase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CHUDWARE_REVENG_DIR"] = self.tmp
        self.tools = _build_tools()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CHUDWARE_REVENG_DIR", None)

    def test_invalid_size(self):
        r = _call(self.tools, "reveng_print_scale_mat", size="A3")
        self.assertFalse(r["ok"])
        self.assertIn("reason", r)



if __name__ == "__main__":
    unittest.main()
