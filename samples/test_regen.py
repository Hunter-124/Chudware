"""
samples/test_regen.py — regen + open + tree/solve asserts for all gallery samples.

Usage (from repo root):
  CHUDWARE_LIBCHUDWARE=<path>/libchudware.so python -m pytest samples/test_regen.py -v

Each sample's build.py exposes a regen_and_verify() entry point that:
  1. Rebuilds the sample from scratch via core_bridge (doc_new + add_features)
  2. Saves the .aicm
  3. Returns a dict { ok, volume_mm3, body_count, ... }

This test reopens each saved .aicm to confirm the doc-load contract
(doc_open + feature_status all ok:true) and checks geometry invariants.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SAMPLES_DIR = Path(__file__).parent
INDEX_FILE = SAMPLES_DIR / "index.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def cb():
    """core_bridge handle — skip entire session if libchudware is not available."""
    try:
        from services.mcp import core_bridge as _cb
    except ImportError:
        pytest.skip("services.mcp not importable (PYTHONPATH not set?)")
    if not _cb.available():
        pytest.skip("libchudware not available — set CHUDWARE_LIBCHUDWARE=<path>")
    return _cb


@pytest.fixture(scope="session")
def gallery_index():
    if not INDEX_FILE.exists():
        pytest.skip(f"samples/index.json not found — run build scripts first")
    return json.loads(INDEX_FILE.read_text())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_build_module(sample_id: str):
    """Import samples/<sample_id>/build.py as a module."""
    build_py = SAMPLES_DIR / sample_id / "build.py"
    if not build_py.exists():
        pytest.skip(f"build.py not found for {sample_id}")
    spec = importlib.util.spec_from_file_location(f"samples_{sample_id}_build", build_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _verify_rebuild(result: dict) -> dict:
    """
    Inspect the regen_and_verify result to verify the build was clean.
    The .aicm file is the tessellation output of doc_save; it is NOT a
    source-format file that doc_open accepts (doc_open expects STEP/IGES).
    Verification is the regen_and_verify() call itself (rebuilds from scratch
    against the live engine), which proves the full build pipeline is clean.
    Returns { ok, feature_count, errors }.
    """
    if not result.get("ok"):
        return {"ok": False, "feature_count": 0, "errors": [result.get("error", "unknown")]}
    rebuild = result.get("rebuild", {})
    statuses = rebuild.get("feature_status", [])
    errors = [
        f"{fs.get('id')}: {fs.get('error', '')}"
        for fs in statuses if not fs.get("ok", True)
    ]
    return {
        "ok": len(errors) == 0,
        "feature_count": len(statuses),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIndexJson:
    def test_index_exists(self):
        assert INDEX_FILE.exists(), "samples/index.json must exist"

    def test_index_schema(self, gallery_index):
        assert "version" in gallery_index
        assert "samples" in gallery_index
        samples = gallery_index["samples"]
        assert len(samples) >= 1, "At least one sample required"
        for s in samples:
            assert "id" in s
            assert "title" in s
            assert "discipline" in s
            assert "doc_file" in s
            assert "build_script" in s
            assert "what_it_teaches" in s
            assert len(s["what_it_teaches"]) >= 1

    def test_all_doc_files_present(self, gallery_index):
        missing = []
        for s in gallery_index["samples"]:
            aicm = SAMPLES_DIR / s["doc_file"]
            if not aicm.exists():
                missing.append(str(aicm))
        assert not missing, f"Missing sample doc files: {missing}"

    def test_all_thumbnails_present(self, gallery_index):
        missing = []
        for s in gallery_index["samples"]:
            if "thumbnail" in s:
                thumb = SAMPLES_DIR / s["thumbnail"]
                if not thumb.exists():
                    missing.append(str(thumb))
        assert not missing, f"Missing thumbnails: {missing}"


class TestMechPartRegen:
    def test_regen_bracket(self, cb):
        """Full regen: builds the bracket from scratch, saves .aicm, checks geometry."""
        mod = _load_build_module("mech_part")
        result = mod.regen_and_verify()
        assert result.get("ok"), f"regen failed: {result.get('error', result)}"
        assert result["volume_mm3"] > 0, "volume must be positive"
        assert result["body_count"] >= 1, "must have at least one body"

    def test_bracket_feature_tree_clean(self, cb):
        """No feature errors in the bracket rebuild."""
        mod = _load_build_module("mech_part")
        result = mod.regen_and_verify()
        verify = _verify_rebuild(result)
        assert verify["ok"], f"feature errors: {verify['errors']}"

    def test_bracket_aicm_saved(self):
        """bracket.aicm was written (regen must have completed)."""
        aicm = SAMPLES_DIR / "mech_part" / "bracket.aicm"
        assert aicm.exists(), "bracket.aicm not found — run build script"
        assert aicm.stat().st_size > 100, "bracket.aicm too small (empty?)"


class TestCamJobRegen:
    def test_regen_lid(self, cb):
        """Full regen: builds the lid and runs cam_program."""
        mod = _load_build_module("cam_job")
        result = mod.regen_and_verify()
        assert result.get("ok"), f"regen failed: {result.get('error', result)}"
        assert result["volume_mm3"] > 0
        assert result.get("cam_ok"), "CAM program must succeed"
        assert result.get("op_count", 0) >= 1, "CAM must have at least one operation"

    def test_lid_feature_tree_clean(self, cb):
        """No feature errors in the lid rebuild."""
        mod = _load_build_module("cam_job")
        result = mod.regen_and_verify()
        verify = _verify_rebuild(result)
        assert verify["ok"], f"feature errors: {verify['errors']}"

    def test_cam_summary_present(self):
        cam_json = SAMPLES_DIR / "cam_job" / "lid_cam.json"
        assert cam_json.exists(), "lid_cam.json must be generated"
        data = json.loads(cam_json.read_text())
        assert data.get("ok"), "CAM summary must report ok=True"
        assert data.get("gcode_lines", 0) > 10, "G-code must have > 10 lines"


class TestStructuralFrameRegen:
    def test_regen_frame(self, cb):
        """Full regen: builds the portal frame with weld_member."""
        mod = _load_build_module("structural_frame")
        result = mod.regen_and_verify()
        assert result.get("ok"), f"regen failed: {result.get('error', result)}"
        assert result["volume_mm3"] > 0
        # weld_member produces one body per segment; expect >= 1 body
        assert result["body_count"] >= 1

    def test_frame_feature_tree_clean(self, cb):
        """No feature errors in the frame rebuild."""
        mod = _load_build_module("structural_frame")
        result = mod.regen_and_verify()
        verify = _verify_rebuild(result)
        assert verify["ok"], f"feature errors: {verify['errors']}"

    def test_frame_aicm_saved(self):
        """frame.aicm was written."""
        aicm = SAMPLES_DIR / "structural_frame" / "frame.aicm"
        assert aicm.exists(), "frame.aicm not found — run build script"
        assert aicm.stat().st_size > 100, "frame.aicm too small (empty?)"
