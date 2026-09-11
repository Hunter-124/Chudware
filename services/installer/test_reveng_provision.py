"""RE-06: Reverse Engineering provisioning descriptors + reveng_status probe.

Offline-only. No network, no downloads. Verifies the descriptor schema is valid,
the readiness probe matches the frozen contract shape (contracts/reveng.md §8.1),
and that with nothing provisioned the probe returns a structured missing-list.
"""
from __future__ import annotations

import builtins

import pytest

from services.vision import reveng_provision as rp

# Frozen §8.1 key sets.
CAPABILITY_KEYS = {"sparse", "dense", "splat", "single_photo", "webcam", "gpu"}
PROVISIONING_KEYS = {"colmap", "openmvs", "brush", "triposr", "vggt", "docling"}


def test_openmvs_is_agpl_discovery_only():
    """AGPL guardrail: OpenMVS must be subprocess/discovery-only, never fetched."""
    d = rp.REVENG_DESCRIPTORS["openmvs"]
    assert d["license"] == "AGPL-3.0"
    assert d["mode"] == "env", "OpenMVS (AGPL) must not be a download descriptor"
    assert d.get("url") is None, "OpenMVS (AGPL) must have no fetch URL"
    assert d["env_var"] == "OPENMVS_BIN"


# --- readiness probe --------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate env, Python-package, managed-tool, and PATH discovery from the host."""
    for var in ("COLMAP_BIN", "OPENMVS_BIN", "BRUSH_BIN", "MESHROOM_BIN",
                "TRIPOSR_WEIGHTS", "TRELLIS_WEIGHTS", "VGGT_WEIGHTS",
                "CAD_RECODE_WEIGHTS", "CHUDWARE_TEST_CAMERA", "CUDA_VISIBLE_DEVICES"):
        monkeypatch.delenv(var, raising=False)
    # neutralize PATH-based tool discovery so the offline assertions are deterministic
    monkeypatch.setattr(rp.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    real_import = builtins.__import__

    def _unavailable_packages(name, *args, **kwargs):
        if name in {"docling", "pycolmap"}:
            raise ImportError(f"{name} unavailable in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _unavailable_packages)
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
    # Package and managed-tool discovery are isolated by clean_env; hide device nodes.
    monkeypatch.setattr(rp.os.path, "exists", lambda *_a, **_k: False)

    st = rp.reveng_status()
    assert set(st["missing"]) == PROVISIONING_KEYS, st["missing"]
    assert all(v is False for v in st["provisioning"].values())
    assert all(v is False for v in st["capabilities"].values())
    assert st["banners"], "should surface fix-it banners when everything is missing"


def test_env_provisioned_flips_available(clean_env, monkeypatch, tmp_path):
    """OpenMVS can be installed without its required sparse reconstruction runtime."""
    executable = tmp_path / "DensifyPointCloud"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setenv("OPENMVS_BIN", str(executable))
    st = rp.reveng_status()
    assert st["provisioning"]["openmvs"] is True
    assert "openmvs" not in st["missing"]
    assert st["capabilities"]["dense"] is False


def test_colmap_binary_does_not_replace_pycolmap(clean_env, monkeypatch, tmp_path):
    """Installed native tools cannot enable workflows without the Python SfM runtime."""
    executable = tmp_path / "colmap"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setenv("COLMAP_BIN", str(executable))
    monkeypatch.setenv("OPENMVS_BIN", str(executable))
    brush = rp.brush_install_dir() / "brush_app"
    brush.parent.mkdir(parents=True)
    brush.write_text("#!/bin/sh\n")
    brush.chmod(0o755)

    st = rp.reveng_status()
    assert st["provisioning"]["brush"] is True
    assert st["provisioning"]["openmvs"] is True
    assert st["provisioning"]["colmap"] is False
    assert "colmap" in st["missing"]
    assert st["capabilities"]["sparse"] is False
    assert st["capabilities"]["dense"] is False
    assert st["capabilities"]["splat"] is False


def test_installer_wrapper_offline(clean_env):
    """provision.reveng_provisioning_status() returns contract-shaped, no network."""
    from services.installer import provision
    st = provision.reveng_provisioning_status()
    assert st["ok"] is True
    assert set(st["capabilities"].keys()) == CAPABILITY_KEYS
    assert "missing" in st
