"""RE-06: Reverse Engineering provisioning descriptors + reveng_status probe.

Offline-only. No network, no downloads. Verifies the descriptor schema is valid,
the readiness probe matches the frozen contract shape (contracts/reveng.md §8.1),
and that with nothing provisioned the probe returns a structured missing-list.
"""
from __future__ import annotations

import os

import pytest

from services.vision import reveng_provision as rp

# Frozen §8.1 key sets.
CAPABILITY_KEYS = {"sparse", "dense", "splat", "single_photo", "webcam", "gpu"}
PROVISIONING_KEYS = {"colmap", "openmvs", "brush", "triposr", "vggt", "docling"}


# --- descriptor schema ------------------------------------------------------

def test_descriptor_schema_valid():
    for name, d in rp.REVENG_DESCRIPTORS.items():
        # every descriptor carries the schema keys, well-formed
        for key in ("kind", "capability", "license", "mode", "sha256"):
            assert key in d, f"{name} missing {key}"
        # sha256 is present (may be the PENDING sentinel / None), never a fabricated hex
        sha = d["sha256"]
        assert sha in (None, rp.SHA_PENDING) or (
            isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha.lower())
        ), f"{name} has a malformed sha256"
        # download-mode descriptors must name a url; env-mode must NOT auto-fetch
        if d["mode"] == "download":
            assert d.get("url"), f"{name} is download mode but has no url"
        if d["mode"] == "env":
            assert d.get("url") is None, f"{name} is env(discovery) mode but has a url"


def test_openmvs_is_agpl_discovery_only():
    """AGPL guardrail: OpenMVS must be subprocess/discovery-only, never fetched."""
    d = rp.REVENG_DESCRIPTORS["openmvs"]
    assert d["license"] == "AGPL-3.0"
    assert d["mode"] == "env", "OpenMVS (AGPL) must not be a download descriptor"
    assert d.get("url") is None, "OpenMVS (AGPL) must have no fetch URL"
    assert d["env_var"] == "OPENMVS_BIN"


def test_no_fabricated_hashes():
    """Any pinned url must carry a real 64-hex sha OR the explicit PENDING sentinel."""
    for name, d in rp.REVENG_DESCRIPTORS.items():
        if d["mode"] == "download":
            sha = d["sha256"]
            assert sha == rp.SHA_PENDING or (isinstance(sha, str) and len(sha) == 64), (
                f"{name}: download descriptor must be PENDING or a real 64-hex sha"
            )


# --- readiness probe --------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    """Strip every provisioning env var so the probe sees a bare, offline machine."""
    for var in ("COLMAP_BIN", "OPENMVS_BIN", "BRUSH_BIN", "MESHROOM_BIN",
                "TRIPOSR_WEIGHTS", "TRELLIS_WEIGHTS", "VGGT_WEIGHTS",
                "CHUDWARE_TEST_CAMERA", "CUDA_VISIBLE_DEVICES"):
        monkeypatch.delenv(var, raising=False)
    # neutralize PATH-based tool discovery so the offline assertions are deterministic
    monkeypatch.setattr(rp.shutil, "which", lambda *_a, **_k: None)
    yield


def test_status_shape(clean_env):
    st = rp.reveng_status()
    assert st["ok"] is True
    assert set(st["capabilities"].keys()) == CAPABILITY_KEYS
    assert set(st["provisioning"].keys()) == PROVISIONING_KEYS
    assert isinstance(st["banners"], list)
    assert all(isinstance(b, str) for b in st["banners"])
    assert isinstance(st["missing"], list)
    # trellis is intentionally NOT a contract provisioning key
    assert "trellis" not in st["provisioning"]


def test_status_offline_all_missing(clean_env, monkeypatch):
    """Bare offline machine -> every provisioning artifact reported missing, structured."""
    # also block the pip-mode docling import and device-node GPU/webcam checks
    monkeypatch.setattr(rp.os.path, "exists", lambda *_a, **_k: False)
    import builtins
    real_import = builtins.__import__

    def _no_docling(name, *a, **k):
        if name == "docling":
            raise ImportError("blocked in test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_docling)

    st = rp.reveng_status()
    assert set(st["missing"]) == PROVISIONING_KEYS, st["missing"]
    assert all(v is False for v in st["provisioning"].values())
    assert all(v is False for v in st["capabilities"].values())
    assert st["banners"], "should surface fix-it banners when everything is missing"


def test_env_provisioned_flips_available(clean_env, monkeypatch, tmp_path):
    """Setting an env var at an existing path marks that capability available."""
    fake = tmp_path / "colmap"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("COLMAP_BIN", str(fake))
    st = rp.reveng_status()
    assert st["provisioning"]["colmap"] is True
    assert "colmap" not in st["missing"]
    assert st["capabilities"]["sparse"] is True


def test_installer_wrapper_offline(clean_env):
    """provision.reveng_provisioning_status() returns contract-shaped, no network."""
    from services.installer import provision
    st = provision.reveng_provisioning_status()
    assert st["ok"] is True
    assert set(st["capabilities"].keys()) == CAPABILITY_KEYS
    assert "missing" in st
