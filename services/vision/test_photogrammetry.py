"""Tests for photogrammetry.py, mesh_cleanup.py and capture_server.py.

All subprocess calls are mocked — no real Meshroom/COLMAP installation needed.
Open3D tests use monkeypatching for the mocked path; pytest.importorskip guards the
real-library path only when the real library is exercised.
"""
from __future__ import annotations

import os
import socket
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# photogrammetry.py tests
# ---------------------------------------------------------------------------
# Use normal package imports so sys.modules and services.vision attributes refer
# to the same modules when other suites patch backend callables.
from services.vision import photogrammetry
from services.vision.photogrammetry import available_backend, reconstruct


class TestAvailableBackend:
    def test_available_backend_none(self, monkeypatch):
        monkeypatch.delenv("MESHROOM_BIN", raising=False)
        monkeypatch.delenv("COLMAP_BIN", raising=False)
        assert available_backend() is None

    def test_available_backend_meshroom(self, monkeypatch):
        monkeypatch.setenv("MESHROOM_BIN", "/usr/bin/meshroom_batch")
        monkeypatch.delenv("COLMAP_BIN", raising=False)
        assert available_backend() == "meshroom"

    def test_available_backend_colmap(self, monkeypatch):
        monkeypatch.delenv("MESHROOM_BIN", raising=False)
        monkeypatch.setenv("COLMAP_BIN", "/usr/bin/colmap")
        assert available_backend() == "colmap"

    def test_meshroom_takes_priority_over_colmap(self, monkeypatch):
        monkeypatch.setenv("MESHROOM_BIN", "/usr/bin/meshroom_batch")
        monkeypatch.setenv("COLMAP_BIN", "/usr/bin/colmap")
        assert available_backend() == "meshroom"


class TestReconstructUnavailable:
    def test_reconstruct_unavailable_graceful(self, monkeypatch):
        monkeypatch.delenv("MESHROOM_BIN", raising=False)
        monkeypatch.delenv("COLMAP_BIN", raising=False)
        with tempfile.TemporaryDirectory() as img_dir, tempfile.TemporaryDirectory() as out_dir:
            result = reconstruct(img_dir, out_dir)
        assert result["ok"] is False
        assert "no photogrammetry backend" in result["reason"]
        assert "hint" in result


class TestReconstructMeshroomMocked:
    def test_reconstruct_meshroom_mocked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MESHROOM_BIN", "/fake/meshroom_batch")
        monkeypatch.delenv("COLMAP_BIN", raising=False)

        img_dir = str(tmp_path / "images")
        os.makedirs(img_dir)
        out_dir = str(tmp_path / "out")
        os.makedirs(out_dir)

        # side_effect writes a fake .obj so the glob can find it
        def fake_run(cmd, **kwargs):
            fake_mesh = Path(out_dir) / "TexturedMesh.obj"
            fake_mesh.write_text("# fake obj\n")
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            result = reconstruct(img_dir, out_dir, scale_mm=100.0)

        assert result["ok"] is True
        assert result["backend"] == "meshroom"
        assert result["mesh_path"].endswith(".obj")
        assert result["scale_mm"] == 100.0


class TestReconstructColmapMocked:
    def test_reconstruct_colmap_mocked(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MESHROOM_BIN", raising=False)
        monkeypatch.setenv("COLMAP_BIN", "/fake/colmap")

        img_dir = str(tmp_path / "images")
        os.makedirs(img_dir)
        out_dir = str(tmp_path / "out")
        os.makedirs(out_dir)

        def fake_run(cmd, **kwargs):
            sparse_dir = Path(out_dir) / "sparse" / "0"
            sparse_dir.mkdir(parents=True)
            fake_mesh = sparse_dir / "fused.ply"
            fake_mesh.write_text("# fake ply\n")
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("subprocess.run", side_effect=fake_run):
            result = reconstruct(img_dir, out_dir)

        assert result["ok"] is True
        assert result["backend"] == "colmap"
        assert result["mesh_path"].endswith(".ply")

    def test_reconstruct_subprocess_failure(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MESHROOM_BIN", "/fake/meshroom_batch")
        monkeypatch.delenv("COLMAP_BIN", raising=False)
        img_dir = str(tmp_path / "images")
        os.makedirs(img_dir)
        out_dir = str(tmp_path / "out")
        os.makedirs(out_dir)

        def fake_run_fail(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 1
            r.stderr = "something went wrong"
            return r

        with patch("subprocess.run", side_effect=fake_run_fail):
            result = reconstruct(img_dir, out_dir)

        assert result["ok"] is False
        assert "something went wrong" in result["reason"]


# ---------------------------------------------------------------------------
# mesh_cleanup.py tests
# ---------------------------------------------------------------------------
from services.vision import mesh_cleanup
from services.vision.mesh_cleanup import cleanup_mesh


class TestCleanupMeshNoOpen3d:
    def test_cleanup_mesh_no_open3d(self, monkeypatch):
        """When open3d is absent, returns {ok: False} dict, not exception."""
        monkeypatch.setattr(mesh_cleanup, "HAVE_OPEN3D", False)
        result = cleanup_mesh("/fake/mesh.obj", "/fake/out.obj")
        assert result["ok"] is False
        assert "open3d" in result["reason"]
        assert "hint" in result


class TestCleanupMeshOpen3dMocked:
    @staticmethod
    def _fake_open3d(monkeypatch, *, extent, n_vertices, n_triangles):
        """Wire a mocked open3d whose mesh has the given bbox extent and counts."""
        fake_aabb = MagicMock()
        fake_aabb.get_extent.return_value = list(extent)
        fake_aabb.get_center.return_value = [0.0, 0.0, 0.0]

        fake_mesh = MagicMock()
        fake_mesh.get_axis_aligned_bounding_box.return_value = fake_aabb
        fake_mesh.vertices = [None] * n_vertices
        fake_mesh.triangles = [None] * n_triangles

        fake_o3d = MagicMock()
        fake_o3d.io.read_triangle_mesh.return_value = fake_mesh

        monkeypatch.setattr(mesh_cleanup, "o3d", fake_o3d)
        monkeypatch.setattr(mesh_cleanup, "HAVE_OPEN3D", True)
        return fake_o3d, fake_mesh

    def test_cleanup_mesh_open3d_mocked(self, monkeypatch, tmp_path):
        """Mock open3d, assert cleanup routines called + returns ok."""
        fake_o3d, fake_mesh = self._fake_open3d(
            monkeypatch, extent=[150.0, 100.0, 80.0], n_vertices=1200, n_triangles=2400
        )

        out_path = str(tmp_path / "cleaned.obj")
        result = cleanup_mesh("/fake/input.obj", out_path, target_longest_mm=300.0)

        assert result["ok"] is True
        assert result["n_vertices"] == 1200
        assert result["n_triangles"] == 2400
        assert result["scale_applied"] is True
        assert result["scale_mode"] == "target_longest_mm"
        fake_mesh.remove_degenerate_triangles.assert_called_once()
        fake_mesh.remove_duplicated_vertices.assert_called_once()
        fake_mesh.remove_unreferenced_vertices.assert_called_once()
        fake_mesh.scale.assert_called_once()
        fake_o3d.io.write_triangle_mesh.assert_called_once()
        # target_longest_mm normalises: 300 mm target / 150-unit longest extent = 2x.
        assert fake_mesh.scale.call_args[0][0] == pytest.approx(2.0)

    def test_cleanup_mesh_mm_per_unit_multiplies(self, monkeypatch, tmp_path):
        """mm_per_unit is a ratio: it multiplies, it does NOT normalise the bbox."""
        _fake_o3d, fake_mesh = self._fake_open3d(
            monkeypatch, extent=[150.0, 100.0, 80.0], n_vertices=1200, n_triangles=2400
        )

        out_path = str(tmp_path / "cleaned.obj")
        result = cleanup_mesh("/fake/input.obj", out_path, mm_per_unit=2.5)

        assert result["ok"] is True
        assert result["scale_applied"] is True
        assert result["scale_mode"] == "mm_per_unit"
        # The factor is the ratio itself — never 2.5 / 150 (the normalising semantic).
        assert fake_mesh.scale.call_args[0][0] == pytest.approx(2.5)

    def test_cleanup_mesh_rejects_both_scale_params(self, monkeypatch, tmp_path):
        """The two units are mutually exclusive — supplying both is a caller bug."""
        fake_o3d, fake_mesh = self._fake_open3d(
            monkeypatch, extent=[150.0, 100.0, 80.0], n_vertices=1200, n_triangles=2400
        )

        result = cleanup_mesh(
            "/fake/input.obj",
            str(tmp_path / "cleaned.obj"),
            mm_per_unit=2.5,
            target_longest_mm=300.0,
        )

        assert result["ok"] is False
        assert "mutually exclusive" in result["reason"]
        assert "hint" in result
        # Rejected before any geometry was touched or written.
        fake_o3d.io.read_triangle_mesh.assert_not_called()
        fake_mesh.scale.assert_not_called()
        fake_o3d.io.write_triangle_mesh.assert_not_called()

    def test_cleanup_mesh_no_scale_keeps_input_units(self, monkeypatch, tmp_path):
        """Neither unit supplied → no scaling, and the result says so out loud."""
        _fake_o3d, fake_mesh = self._fake_open3d(
            monkeypatch, extent=[150.0, 100.0, 80.0], n_vertices=1200, n_triangles=2400
        )

        result = cleanup_mesh("/fake/input.obj", str(tmp_path / "cleaned.obj"))

        assert result["ok"] is True
        assert result["scale_applied"] is False
        assert result["scale_mode"] is None
        fake_mesh.scale.assert_not_called()
        assert any("input units" in n for n in result["notes"])

    def test_cleanup_mesh_rejects_nonpositive_scale(self, monkeypatch, tmp_path):
        """A zero/negative factor would collapse or mirror the mesh — refuse it."""
        fake_o3d, _fake_mesh = self._fake_open3d(
            monkeypatch, extent=[150.0, 100.0, 80.0], n_vertices=1200, n_triangles=2400
        )

        result = cleanup_mesh("/fake/input.obj", str(tmp_path / "cleaned.obj"), mm_per_unit=0.0)

        assert result["ok"] is False
        assert "positive" in result["reason"]
        assert "hint" in result
        fake_o3d.io.write_triangle_mesh.assert_not_called()

    def test_cleanup_mesh_low_confidence_note(self, monkeypatch, tmp_path):
        """Vertex count below threshold triggers low-confidence note."""
        self._fake_open3d(
            monkeypatch, extent=[10.0, 8.0, 5.0], n_vertices=50, n_triangles=80
        )

        out_path = str(tmp_path / "cleaned.obj")
        result = cleanup_mesh("/fake/input.obj", out_path)

        assert result["ok"] is True
        assert "notes" in result
        assert any("low reconstruction confidence" in n for n in result["notes"])


# ---------------------------------------------------------------------------
# capture_server.py tests
# ---------------------------------------------------------------------------
from services.vision import capture_server
from services.vision.capture_server import (
    CaptureSession,
    get_guided_shot_names,
    start_session,
    stop_session,
)


class TestCaptureSessionStartStop:
    def test_get_guided_shot_names(self):
        names = get_guided_shot_names()
        assert len(names) == 11
        assert "top" in names
        assert "front" in names
        assert "bottom" in names

    def test_start_session_generates_url(self, tmp_path):
        """start_session creates a session with an upload URL; no real network needed."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            assert session.token
            assert len(session.token) >= 16
            assert str(port) in session.upload_url
            assert "token=" in session.upload_url
            assert session.work_dir is not None
        finally:
            if session is not None:
                stop_session(session)

    def test_stop_session_when_no_received(self, tmp_path):
        """Stopping a session with no uploaded photos is graceful."""
        port = _free_port()
        session = start_session(port=port, bind_host="127.0.0.1")
        assert session.received == []
        stop_session(session)  # should not raise

    def test_qr_returns_string(self, tmp_path):
        """QR property always returns a non-empty string (URL or data URL)."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            qr = session.qr
            assert isinstance(qr, str)
            assert len(qr) > 10
        finally:
            if session is not None:
                stop_session(session)


class TestTokenRequired:
    def test_get_without_token_returns_401(self):
        """HTTP GET to the upload endpoint without a token returns 401."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            time.sleep(0.1)  # give the thread a moment to bind
            url = f"http://127.0.0.1:{port}/upload"  # no token param
            req = urllib.request.Request(url)
            try:
                urllib.request.urlopen(req, timeout=3)
                pytest.fail("Expected HTTP 401 but got 200")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
        finally:
            if session is not None:
                stop_session(session)

    def test_get_with_wrong_token_returns_401(self):
        """HTTP GET with a wrong token returns 401."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            time.sleep(0.1)
            url = f"http://127.0.0.1:{port}/upload?token=wrongtoken123"
            req = urllib.request.Request(url)
            try:
                urllib.request.urlopen(req, timeout=3)
                pytest.fail("Expected HTTP 401 but got 200")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
        finally:
            if session is not None:
                stop_session(session)

    def test_get_with_correct_token_returns_200(self):
        """HTTP GET with the correct token returns the guided page (200)."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            time.sleep(0.1)
            url = f"http://127.0.0.1:{port}/?token={session.token}"
            resp = urllib.request.urlopen(url, timeout=3)
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "Chudware Photo Capture" in body
        finally:
            if session is not None:
                stop_session(session)


class TestPhotoUpload:
    """Cover the _parse_multipart_photo / upload POST path — the most recently rewritten piece
    (cgi -> email-parser in d6e3b8d).  No real phone or image library needed: we POST raw
    multipart bytes to the live local server and assert the files land correctly."""

    @staticmethod
    def _build_multipart(field_name: str, filename: str, data: bytes,
                         content_type: str = "image/jpeg") -> tuple:
        """Return (body_bytes, content_type_header) for a minimal multipart/form-data body."""
        boundary = "----TestBoundary1234567890"
        lines = []
        lines.append(f"--{boundary}".encode())
        lines.append(
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"'.encode()
        )
        lines.append(f"Content-Type: {content_type}".encode())
        lines.append(b"")
        lines.append(data)
        lines.append(f"--{boundary}--".encode())
        body = b"\r\n".join(lines) + b"\r\n"
        ctype = f"multipart/form-data; boundary={boundary}"
        return body, ctype

    def test_post_photo_lands_in_work_dir(self):
        """POST a fake JPEG to the upload endpoint — asserts 200, file saved, received grows."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            time.sleep(0.1)

            fake_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16  # minimal JPEG magic bytes
            body, ctype = self._build_multipart("photo", "test_photo.jpg", fake_jpeg)

            url = f"http://127.0.0.1:{port}/upload?token={session.token}"
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": ctype, "Content-Length": str(len(body))},
            )
            resp = urllib.request.urlopen(req, timeout=3)
            assert resp.status == 200

            # File must be in work_dir with index + shot-name
            assert len(session.received) == 1
            from pathlib import Path as _Path
            saved = _Path(session.received[0])
            assert saved.exists()
            assert saved.read_bytes() == fake_jpeg
            assert saved.name.startswith("000_top")
            assert saved.suffix == ".jpg"
        finally:
            if session is not None:
                stop_session(session)

    def test_post_two_photos_advance_index_and_shot_name(self):
        """Two sequential POSTs produce 000_top.jpg and 001_front.jpg in order."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            time.sleep(0.1)

            for i, (fname, expected_stem_prefix) in enumerate([
                ("photo1.jpg", "000_top"),
                ("photo2.jpg", "001_front"),
            ]):
                fake_data = b"\xff\xd8\xff\xe0" + bytes([i]) * 16
                body, ctype = self._build_multipart("photo", fname, fake_data)
                url = f"http://127.0.0.1:{port}/upload?token={session.token}"
                req = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": ctype, "Content-Length": str(len(body))},
                )
                resp = urllib.request.urlopen(req, timeout=3)
                assert resp.status == 200

            assert len(session.received) == 2
            from pathlib import Path as _Path
            names = [_Path(p).name for p in session.received]
            assert names[0].startswith("000_top")
            assert names[1].startswith("001_front")
        finally:
            if session is not None:
                stop_session(session)

    def test_post_without_token_returns_401(self):
        """POST to /upload without a token returns 401 and does not save anything."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            time.sleep(0.1)

            body, ctype = self._build_multipart("photo", "x.jpg", b"\xff\xd8\xff\xe0")
            url = f"http://127.0.0.1:{port}/upload"  # no token
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": ctype, "Content-Length": str(len(body))},
            )
            try:
                urllib.request.urlopen(req, timeout=3)
                pytest.fail("Expected 401 but got 200")
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
            assert len(session.received) == 0
        finally:
            if session is not None:
                stop_session(session)

    def test_post_png_extension_preserved(self):
        """PNG filename extension is kept (not forced to .jpg)."""
        port = _free_port()
        session = None
        try:
            session = start_session(port=port, bind_host="127.0.0.1")
            time.sleep(0.1)

            fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
            body, ctype = self._build_multipart("photo", "snap.png", fake_png,
                                                content_type="image/png")
            url = f"http://127.0.0.1:{port}/upload?token={session.token}"
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": ctype, "Content-Length": str(len(body))},
            )
            resp = urllib.request.urlopen(req, timeout=3)
            assert resp.status == 200

            from pathlib import Path as _Path
            saved = _Path(session.received[0])
            assert saved.suffix == ".png"
        finally:
            if session is not None:
                stop_session(session)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
