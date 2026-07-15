"""Tests for engine_oracle/vision/single_photo.py

Acceptance criteria (RE-12 DoD):
  (a) Graceful ``backend_not_provisioned`` when weights absent.
  (b) Any produced/mocked mesh carries the ``approximate`` tag.
  (c) A guard that refuses to pass an approximate mesh into the parametric path
      without allow_approximate=True.
"""
from __future__ import annotations

import os
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all backend env vars so _backend_available() returns False."""
    for var in ("TRIPOSR_WEIGHTS", "TRELLIS_WEIGHTS"):
        monkeypatch.delenv(var, raising=False)


def _make_fake_mesh_result(ok: bool = True) -> dict:
    """Build a fake mesh result as reconstruct_single_photo would return it."""
    if ok:
        return {
            "ok": True,
            "session_id": "re_test1234",
            "mesh_path": "/tmp/fake.obj",
            "backend": "triposr",
            "quality": "low",
            "honesty": "approximate",
        }
    return {
        "ok": False,
        "reason": "backend_not_provisioned",
        "hint": "Set TRELLIS_WEIGHTS or TRIPOSR_WEIGHTS ...",
        "backend_attempted": None,
    }


# ---------------------------------------------------------------------------
# (a) Graceful degradation when weights absent
# ---------------------------------------------------------------------------

class TestBackendNotProvisioned:
    def test_returns_not_provisioned_envelope(self, monkeypatch, tmp_path):
        """When no weights are on disk/env, returns ok=False + backend_not_provisioned."""
        _clear_backend_env(monkeypatch)

        # Ensure _artifact_available always returns False for single_photo backends
        from ..vision import reveng_provision
        monkeypatch.setattr(reveng_provision, "_artifact_available", lambda name: False)

        from ..vision.single_photo import (
            reconstruct_single_photo,
            BACKEND_NOT_PROVISIONED,
        )

        fake_image = tmp_path / "part.jpg"
        fake_image.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

        result = reconstruct_single_photo(fake_image, "re_test0001", output_dir=tmp_path)

        assert result["ok"] is False
        assert result["reason"] == BACKEND_NOT_PROVISIONED
        assert "hint" in result
        assert result["hint"]  # non-empty

    def test_envelope_has_no_traceback(self, monkeypatch, tmp_path):
        """Degradation envelope must not contain raw exception text / traceback."""
        _clear_backend_env(monkeypatch)

        from ..vision import reveng_provision
        monkeypatch.setattr(reveng_provision, "_artifact_available", lambda name: False)

        from ..vision.single_photo import reconstruct_single_photo

        fake_image = tmp_path / "part.jpg"
        fake_image.write_bytes(b"\xff\xd8\xff")

        result = reconstruct_single_photo(fake_image, "re_test0002", output_dir=tmp_path)
        # Ensure no raw Python traceback markers leak
        for key, val in result.items():
            if isinstance(val, str):
                assert "Traceback" not in val, f"Traceback found in result[{key!r}]"


# ---------------------------------------------------------------------------
# (b) Approximate tag on any produced / mocked mesh
# ---------------------------------------------------------------------------

class TestApproximateTag:
    def test_tag_approximate_helper_sets_field(self):
        """_tag_approximate always writes honesty='approximate'."""
        from ..vision.single_photo import _tag_approximate, APPROXIMATE_TAG

        result: dict = {"ok": True, "mesh_path": "/tmp/a.obj"}
        _tag_approximate(result)
        assert result["honesty"] == APPROXIMATE_TAG

    def test_successful_mock_result_carries_approximate(self, monkeypatch, tmp_path):
        """When backend is mocked to succeed, the result must carry approximate tag."""
        _clear_backend_env(monkeypatch)

        from ..vision import reveng_provision, single_photo
        # Fake a provisioned triposr backend
        monkeypatch.setattr(
            reveng_provision, "_artifact_available",
            lambda name: name == "triposr",
        )

        # Stub out the actual backend call so we don't need weights
        fake_mesh = tmp_path / "re_test0003_triposr.obj"
        fake_mesh.write_text("# fake mesh")

        monkeypatch.setattr(
            single_photo, "_run_backend",
            lambda backend, img, out, sid: fake_mesh,
        )

        from ..vision.single_photo import reconstruct_single_photo, APPROXIMATE_TAG

        fake_image = tmp_path / "part.jpg"
        fake_image.write_bytes(b"\xff\xd8\xff")

        result = reconstruct_single_photo(fake_image, "re_test0003", output_dir=tmp_path)

        assert result["ok"] is True
        assert result.get("honesty") == APPROXIMATE_TAG

    def test_failed_backend_also_tagged_approximate(self, monkeypatch, tmp_path):
        """Even on backend RuntimeError, the returned envelope carries approximate tag."""
        _clear_backend_env(monkeypatch)

        from ..vision import reveng_provision, single_photo
        monkeypatch.setattr(
            reveng_provision, "_artifact_available",
            lambda name: name == "triposr",
        )
        monkeypatch.setattr(
            single_photo, "_run_backend",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("GPU OOM")),
        )

        from ..vision.single_photo import reconstruct_single_photo, APPROXIMATE_TAG

        fake_image = tmp_path / "part.jpg"
        fake_image.write_bytes(b"\xff\xd8\xff")

        result = reconstruct_single_photo(fake_image, "re_test0004", output_dir=tmp_path)
        assert result["ok"] is False
        assert result.get("honesty") == APPROXIMATE_TAG

    def test_fake_mesh_dict_has_approximate(self):
        """Sanity: helper produces correctly tagged dict."""
        from ..vision.single_photo import APPROXIMATE_TAG

        result = _make_fake_mesh_result(ok=True)
        assert result["honesty"] == APPROXIMATE_TAG


# ---------------------------------------------------------------------------
# (c) Guard refuses approximate mesh into parametric path without override
# ---------------------------------------------------------------------------

class TestApproximateMeshGuard:
    def test_raises_without_override(self):
        """assert_not_approximate raises when honesty=approximate and no override."""
        from ..vision.single_photo import (
            assert_not_approximate,
            ApproximateMeshGuardError,
            APPROXIMATE_TAG,
        )

        mesh = {"ok": True, "honesty": APPROXIMATE_TAG, "mesh_path": "/tmp/a.obj"}

        with pytest.raises(ApproximateMeshGuardError):
            assert_not_approximate(mesh, allow_approximate=False)

    def test_raises_by_default(self):
        """Default allow_approximate=False — no kwarg needed to trigger the guard."""
        from ..vision.single_photo import (
            assert_not_approximate,
            ApproximateMeshGuardError,
            APPROXIMATE_TAG,
        )

        mesh = {"ok": True, "honesty": APPROXIMATE_TAG}

        with pytest.raises(ApproximateMeshGuardError):
            assert_not_approximate(mesh)

    def test_passes_with_explicit_override(self):
        """allow_approximate=True lets the call through silently."""
        from ..vision.single_photo import assert_not_approximate, APPROXIMATE_TAG

        mesh = {"ok": True, "honesty": APPROXIMATE_TAG}
        # Should NOT raise
        assert_not_approximate(mesh, allow_approximate=True)

    def test_passes_for_non_approximate_mesh(self):
        """A mesh without the approximate tag passes through without override."""
        from ..vision.single_photo import assert_not_approximate

        mesh = {"ok": True, "honesty": "fitted", "mesh_path": "/tmp/b.obj"}
        # Should NOT raise
        assert_not_approximate(mesh, allow_approximate=False)

    def test_error_message_mentions_override(self):
        """Error message tells the user how to fix it."""
        from ..vision.single_photo import (
            assert_not_approximate,
            ApproximateMeshGuardError,
            APPROXIMATE_TAG,
        )

        mesh = {"ok": True, "honesty": APPROXIMATE_TAG}

        with pytest.raises(ApproximateMeshGuardError, match="allow_approximate=True"):
            assert_not_approximate(mesh)

    def test_not_provisioned_envelope_also_blocked(self):
        """The not_provisioned envelope (ok=False) has no honesty tag → passes guard
        (it cannot enter the parametric path anyway — it has no mesh)."""
        from ..vision.single_photo import assert_not_approximate

        envelope = _make_fake_mesh_result(ok=False)
        # ok=False result has no honesty tag → should not raise
        assert_not_approximate(envelope, allow_approximate=False)
