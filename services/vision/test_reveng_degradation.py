"""RE-26: degradation-matrix tests for reveng_status() banners.

Simulates no-GPU, no-binary, and no-webcam environments via monkeypatching
_gpu_available, _webcam_available, and _artifact_available in reveng_provision.
Also asserts that contracts/license_debt.md contains the required RE-26 entries
(OpenMVS AGPL-3.0 and CAD-Recode CC-BY-NC).
"""
from __future__ import annotations

import os
import pathlib
import importlib
import sys
import types
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
from ..vision import reveng_provision as rp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_status(**overrides) -> dict:
    """Call reveng_status() with selected internals monkeypatched.

    Keyword args accepted:
      gpu (bool)      - override _gpu_available()
      webcam (bool)   - override _webcam_available()
      binaries (dict) - map of artifact-name -> bool, fed to _artifact_available()
    """
    gpu = overrides.get("gpu", True)
    webcam = overrides.get("webcam", True)
    binaries: dict[str, bool] = overrides.get("binaries", {})

    def _fake_artifact(name: str) -> bool:
        return binaries.get(name, True)

    with (
        mock.patch.object(rp, "_gpu_available", return_value=gpu),
        mock.patch.object(rp, "_webcam_available", return_value=webcam),
        mock.patch.object(rp, "_artifact_available", side_effect=_fake_artifact),
    ):
        return rp.reveng_status()


# ---------------------------------------------------------------------------
# Scenario: all available (baseline)
# ---------------------------------------------------------------------------

def test_full_availability():
    status = _fresh_status(
        gpu=True,
        webcam=True,
        binaries={k: True for k in rp._PROVISIONING_KEYS},
    )
    assert status["ok"] is True
    caps = status["capabilities"]
    assert caps["gpu"] is True
    assert caps["webcam"] is True
    assert caps["sparse"] is True
    assert caps["dense"] is True
    assert caps["splat"] is True
    assert caps["single_photo"] is True
    assert status["banners"] == []
    assert status["missing"] == []


# ---------------------------------------------------------------------------
# Scenario: no GPU
# ---------------------------------------------------------------------------

def test_no_gpu_capability_reported():
    status = _fresh_status(
        gpu=False,
        webcam=True,
        binaries={k: True for k in rp._PROVISIONING_KEYS},
    )
    assert status["capabilities"]["gpu"] is False
    # Reconstruction backends (CPU-capable for small scenes) still reported available
    # when their binaries are present — GPU flag is advisory only.
    assert status["capabilities"]["sparse"] is True


def test_no_gpu_no_banner_for_gpu_itself():
    """reveng_status does not emit a banner for missing GPU; it's informational only."""
    status = _fresh_status(gpu=False, webcam=True,
                           binaries={k: True for k in rp._PROVISIONING_KEYS})
    # No banner should mention GPU (GPU absence is in capabilities, not banners)
    for banner in status["banners"]:
        assert "GPU" not in banner.upper() or "AGPL" in banner


# ---------------------------------------------------------------------------
# Scenario: no webcam
# ---------------------------------------------------------------------------

def test_no_webcam_capability_reported():
    status = _fresh_status(
        gpu=True,
        webcam=False,
        binaries={k: True for k in rp._PROVISIONING_KEYS},
    )
    assert status["capabilities"]["webcam"] is False


# ---------------------------------------------------------------------------
# Scenario: no reconstruction binaries
# ---------------------------------------------------------------------------

def test_no_colmap_sparse_unavailable():
    status = _fresh_status(
        gpu=True, webcam=True,
        binaries={k: False for k in rp._PROVISIONING_KEYS},
    )
    assert status["capabilities"]["sparse"] is False
    banners_text = " ".join(status["banners"])
    assert "COLMAP" in banners_text or "Sparse" in banners_text


def test_no_openmvs_dense_unavailable():
    """Dense requires both colmap (sparse) + openmvs."""
    status = _fresh_status(
        gpu=True, webcam=True,
        binaries={k: (k == "colmap") for k in rp._PROVISIONING_KEYS},
    )
    assert status["capabilities"]["dense"] is False


def test_no_brush_splat_unavailable():
    status = _fresh_status(
        gpu=True, webcam=True,
        binaries={k: (k != "brush") for k in rp._PROVISIONING_KEYS},
    )
    assert status["capabilities"]["splat"] is False
    banners_text = " ".join(status["banners"])
    assert "Brush" in banners_text or "Splatting" in banners_text


def test_no_single_photo_weights_unavailable():
    """single_photo is False when neither triposr nor trellis are available."""
    all_off = {k: False for k in rp._PROVISIONING_KEYS}
    all_off["trellis"] = False  # _artifact_available for trellis (not in _PROVISIONING_KEYS)
    status = _fresh_status(gpu=True, webcam=True, binaries=all_off)
    assert status["capabilities"]["single_photo"] is False
    banners_text = " ".join(status["banners"])
    assert "TripoSR" in banners_text or "TRELLIS" in banners_text or "single" in banners_text.lower()


# ---------------------------------------------------------------------------
# Scenario: combined no-GPU + no-webcam + no-binaries (full offline degraded)
# ---------------------------------------------------------------------------

def test_fully_degraded():
    # "trellis" is not in _PROVISIONING_KEYS but is checked inside reveng_status()
    # for single_photo; include it in the binaries override so the scenario is truly
    # all-absent.
    all_off = {k: False for k in rp._PROVISIONING_KEYS}
    all_off["trellis"] = False
    status = _fresh_status(
        gpu=False, webcam=False,
        binaries=all_off,
    )
    caps = status["capabilities"]
    assert caps["gpu"] is False
    assert caps["webcam"] is False
    assert caps["sparse"] is False
    assert caps["dense"] is False
    assert caps["splat"] is False
    assert caps["single_photo"] is False
    # Multiple banners expected
    assert len(status["banners"]) >= 3
    assert len(status["missing"]) == len(rp._PROVISIONING_KEYS)


# ---------------------------------------------------------------------------
# Scenario: missing list is accurate
# ---------------------------------------------------------------------------

def test_missing_list_accurate():
    present = {"colmap", "brush"}
    status = _fresh_status(
        gpu=True, webcam=True,
        binaries={k: (k in present) for k in rp._PROVISIONING_KEYS},
    )
    missing = set(status["missing"])
    expected_missing = set(rp._PROVISIONING_KEYS) - present
    assert missing == expected_missing


# ---------------------------------------------------------------------------
# Contract: response shape matches §8.1
# ---------------------------------------------------------------------------

def test_response_shape():
    status = _fresh_status(gpu=True, webcam=True,
                           binaries={k: True for k in rp._PROVISIONING_KEYS})
    required_top = {"ok", "capabilities", "provisioning", "banners", "missing"}
    assert required_top.issubset(status.keys())

    required_caps = {"sparse", "dense", "splat", "single_photo", "webcam", "gpu"}
    assert required_caps == set(status["capabilities"].keys())

    required_prov = set(rp._PROVISIONING_KEYS)
    assert required_prov == set(status["provisioning"].keys())


# ---------------------------------------------------------------------------
# License debt assertions (RE-26 DoD)
# ---------------------------------------------------------------------------

def _license_debt_text() -> str:
    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "contracts" / "license_debt.md"
    return path.read_text()


def test_license_debt_has_openmvs_agpl():
    text = _license_debt_text()
    assert "OpenMVS" in text, "license_debt.md must have an OpenMVS entry"
    assert "AGPL" in text, "license_debt.md must mention AGPL-3.0 for OpenMVS"


def test_license_debt_has_cad_recode_cc_nc():
    text = _license_debt_text()
    assert "CAD-Recode" in text, "license_debt.md must have a CAD-Recode entry"
    # Accept either form: CC-BY-NC or CC-NC
    assert "CC-BY-NC" in text or "CC-NC" in text, (
        "license_debt.md must record CAD-Recode's CC-BY-NC license"
    )


def test_license_debt_has_vggt_entry():
    text = _license_debt_text()
    assert "VGGT" in text, "license_debt.md must note VGGT's unverified license"


# ---------------------------------------------------------------------------
# OpenMVS banner explicitly mentions AGPL + subprocess
# ---------------------------------------------------------------------------

def test_openmvs_banner_mentions_agpl():
    """Banner for missing OpenMVS must remind user of the AGPL constraint."""
    status = _fresh_status(
        gpu=True, webcam=True,
        binaries={k: (k != "openmvs") for k in rp._PROVISIONING_KEYS},
    )
    agpl_banners = [b for b in status["banners"] if "AGPL" in b]
    assert agpl_banners, (
        "Expected at least one banner mentioning AGPL when OpenMVS is absent"
    )
