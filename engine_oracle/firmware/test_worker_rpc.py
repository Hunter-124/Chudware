"""Tests for the T-FIN5 firmware worker endpoints (engine/firmware/worker_rpc.py)."""

from __future__ import annotations

import pytest

from engine.firmware import worker_rpc


def _emit(_data: dict) -> None:  # worker-style progress sink (unused by fw handlers)
    pass


@pytest.fixture()
def fw_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CAD_FIRMWARE_DIR", str(tmp_path))
    return tmp_path


def test_registry_mirrors_mcp_surface():
    expected = {
        "fw_status", "fw_projects", "fw_new_project", "fw_build", "fw_sources",
        "fw_sim_run", "fw_flash", "fw_debug_start", "fw_debug_halt", "fw_debug_read_mem",
    }
    assert expected == set(worker_rpc.WORKER_METHODS)


def test_fw_status_shape():
    r = worker_rpc.WORKER_METHODS["fw_status"]({}, _emit)
    assert r["ok"] is True
    assert "families" in r and "cortex-m" in r["families"]
    assert isinstance(r["banners"], list)


def test_projects_roundtrip(fw_root):
    assert worker_rpc.WORKER_METHODS["fw_projects"]({}, _emit) == {"projects": []}

    created = worker_rpc.WORKER_METHODS["fw_new_project"](
        {"name": "blinky", "mcu_family": "cortex-m4"}, _emit)
    assert "project_id" in created, created

    listed = worker_rpc.WORKER_METHODS["fw_projects"]({}, _emit)["projects"]
    assert len(listed) == 1
    p = listed[0]
    assert p["project_id"] == created["project_id"]
    assert p["name"] == "blinky"
    assert p["root_dir"].endswith(p["project_id"])

    srcs = worker_rpc.WORKER_METHODS["fw_sources"]({"project_id": p["project_id"]}, _emit)
    assert srcs["src_paths"] == p["src_paths"]


def test_projects_skips_corrupt_sidecar(fw_root):
    bad = fw_root / "corrupt"
    bad.mkdir()
    (bad / "project.json").write_text("{not json", encoding="utf-8")
    assert worker_rpc.WORKER_METHODS["fw_projects"]({}, _emit) == {"projects": []}


def test_error_envelope_passthrough(fw_root):
    r = worker_rpc.WORKER_METHODS["fw_build"]({}, _emit)
    assert r["code"] == "invalid_request"
    r = worker_rpc.WORKER_METHODS["fw_sim_run"]({"project_id": "nope"}, _emit)
    assert r["code"] == "unknown_project"
