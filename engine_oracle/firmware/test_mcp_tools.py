"""Tests for the firmware compile-loop MCP handlers (T-C2): fw_new_project / fw_build / fw_sources."""

from __future__ import annotations

import pytest

from engine.ecad import schema
from engine.ecad import session as ecad_session
from engine.firmware import mcp_tools as fw
from engine.firmware import project as fw_project


@pytest.fixture(autouse=True)
def _isolated_firmware_root(tmp_path, monkeypatch):
    """Point every project at a throwaway dir so load()/save() round-trip in-test."""
    monkeypatch.setenv("AI_CAD_FIRMWARE_DIR", str(tmp_path / "fw"))


def _open_board(*net_names: str, mcu_value: str = "STM32F103") -> str:
    """Register a board doc in the ECAD session registry and return its doc_id (a board_doc_ref)."""
    board = schema.new_board("t_c2_board")
    for nm in net_names:
        board["nets"].append({"name": nm, "class": "default"})
    board["footprints"].append({
        "id": "fp1", "fpid": "MCU:LQFP48", "ref": "U1", "value": mcu_value,
        "pos": [10.0, 10.0], "angle": 0.0, "side": "top", "fields": {}, "pads": [],
    })
    return ecad_session.open_session(board).doc_id


# --------------------------------------------------------------------------- fw_new_project


def test_fw_new_project_explicit_mcu():
    res = fw.fw_new_project({"name": "blink", "mcu_family": "cortex-m4"})
    assert "project_id" in res
    assert res["project"]["mcu_family"] == "cortex-m4"
    assert res["project"]["schema"] == "aicad.firmware.project/1"
    # compile_commands.json guarantee (contract §6) — project loads back
    loaded = fw_project.load(res["project_id"])
    assert loaded.mcu_family == "cortex-m4"


def test_fw_new_project_auto_selects_mcu_from_board_bom():
    ref = _open_board("PA5", "MISO", mcu_value="STM32F103")
    res = fw.fw_new_project({"name": "auto", "board_doc_ref": ref})  # no mcu_family
    assert res["project"]["mcu_family"] == "cortex-m4"
    assert res["project"]["board_doc_ref"] == ref


def test_fw_new_project_seeds_pinmap_from_board():
    ref = _open_board("PA5", "MISO")
    res = fw.fw_new_project({"board_doc_ref": ref, "mcu_family": "cortex-m4"})
    assert "pinmap_path" in res
    from pathlib import Path
    header = Path(res["pinmap_path"]).read_text(encoding="utf-8")
    assert "#define PIN_PA5" in header
    assert "#define PIN_MISO" in header


def test_fw_new_project_errors_without_mcu_or_board():
    res = fw.fw_new_project({"name": "orphan"})   # no mcu, no board
    assert res.get("code") == "invalid_request"
    assert "mcu" in res["message"]


# --------------------------------------------------------------------------- fw_build


def test_fw_build_returns_build_result_shape():
    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    res = fw.fw_build({"project_id": proj["project_id"]})
    # DoD: fw_build returns {success, elf_path, stderr, warnings} — assert the full BuildResult shape.
    for key in ("success", "elf_path", "map_path", "stderr", "warnings", "returncode", "cmd", "ms"):
        assert key in res, f"BuildResult missing '{key}'"
    # CI has no arm-none-eabi cross-toolchain, so a real .elf cannot be produced here.
    assert res["success"] is False
    assert res["returncode"] != 0            # -1 (cmake absent) or non-zero (cross-compile fails)
    assert isinstance(res["warnings"], list)


def test_fw_build_unknown_project_is_error_envelope():
    res = fw.fw_build({"project_id": "deadbeefcafe"})
    assert res.get("code") == "unknown_project"


def test_fw_build_requires_project_id():
    res = fw.fw_build({})
    assert res.get("code") == "invalid_request"


# --------------------------------------------------------------------------- fw_sources


def test_fw_sources_lists_template_sources():
    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    res = fw.fw_sources({"project_id": proj["project_id"]})
    assert "src_paths" in res
    # cmake_arm template ships src/main.c + src/startup.c
    assert any(p.endswith("main.c") for p in res["src_paths"])


def test_fw_sources_add_and_remove():
    proj = fw.fw_new_project({"mcu_family": "cortex-m4"})
    pid = proj["project_id"]

    added = fw.fw_sources({"project_id": pid, "add": ["src/blink.c"]})
    assert "src/blink.c" in added["src_paths"]
    # add materialises a stub so a later build sees it
    loaded = fw_project.load(pid)
    assert (loaded.root_dir / "src/blink.c").is_file()

    removed = fw.fw_sources({"project_id": pid, "remove": ["src/blink.c"]})
    assert "src/blink.c" not in removed["src_paths"]


def test_fw_sources_requires_project_id():
    res = fw.fw_sources({})
    assert res.get("code") == "invalid_request"
