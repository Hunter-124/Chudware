"""Tests for the firmware project record + toolchain wiring (firmware_rpc.md §2/§3/§6).

Runs without any cross-toolchain installed: the build tests mock ``subprocess.run`` / ``shutil.which``
so they exercise the wiring (argv assembly, artifact discovery, graceful tool-not-found) deterministically.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from engine.firmware import project as fw
from engine.firmware import toolchains as tc


@pytest.fixture()
def firmware_home(tmp_path, monkeypatch):
    """Redirect the firmware workspace into a temp dir (AI_CAD_FIRMWARE_DIR override)."""
    monkeypatch.setenv("AI_CAD_FIRMWARE_DIR", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- new_project


def test_new_project_creates_dir_template_and_compile_commands(firmware_home):
    p = fw.new_project(None, "cortex-m4", "cmake")

    assert p.root_dir.is_dir()
    assert (p.root_dir / "CMakeLists.txt").is_file()
    assert (p.root_dir / "src" / "main.c").is_file()
    assert (p.root_dir / "cmake" / "arm-none-eabi-toolchain.cmake").is_file()
    # DoD: the project dir has a compile_commands.json even without a cross-toolchain.
    assert (p.root_dir / "compile_commands.json").is_file()
    assert (p.root_dir / "project.json").is_file()


def test_src_paths_discovered_from_template(firmware_home):
    p = fw.new_project(None, "cortex-m4", "cmake")
    assert "src/main.c" in p.src_paths
    assert "src/startup.c" in p.src_paths


def test_compile_commands_is_valid_and_lists_sources(firmware_home):
    p = fw.new_project(None, "cortex-m4", "cmake")
    entries = json.loads((p.root_dir / "compile_commands.json").read_text())
    assert isinstance(entries, list) and entries
    for e in entries:  # each entry has file/command/directory
        assert {"file", "command", "directory"} <= set(e)
    assert any("main.c" in e["file"] for e in entries)


def test_board_doc_ref_persisted(firmware_home):
    p = fw.new_project("board-abc123", "cortex-m4", "cmake")
    assert p.board_doc_ref == "board-abc123"
    reloaded = fw.load(p.project_id)
    assert reloaded.board_doc_ref == "board-abc123"


def test_save_load_roundtrip(firmware_home):
    p = fw.new_project(None, "esp32", "espidf", name="blinky")
    r = fw.load(p.project_id)
    assert r.project_id == p.project_id
    assert r.name == "blinky"
    assert r.mcu_family == "esp32"
    assert r.toolchain == "espidf"
    assert r.src_paths == p.src_paths


def test_invalid_toolchain_rejected(firmware_home):
    with pytest.raises(ValueError):
        fw.new_project(None, "cortex-m4", "nonsense")


# --------------------------------------------------------------------------- toolchains (mocked)


def test_cmake_build_graceful_when_tool_missing(firmware_home, monkeypatch):
    p = fw.new_project(None, "cortex-m4", "cmake")
    monkeypatch.setattr(tc.shutil, "which", lambda _tool: None)  # no cmake on PATH

    result = tc.build(p)
    assert result.success is False
    assert result.returncode == -1
    assert "toolchain_not_found" in result.stderr
    # never raised, and serializes to the contract dict shape
    d = result.to_dict()
    assert set(d) == {"success", "elf_path", "map_path", "stderr", "warnings",
                      "returncode", "cmd", "ms"}


def test_arduino_and_espidf_missing_tool_graceful(firmware_home, monkeypatch):
    monkeypatch.setattr(tc.shutil, "which", lambda _tool: None)
    for tool, mcu in (("arduino", "arduino:avr:uno"), ("espidf", "esp32")):
        p = fw.new_project(None, mcu, tool)
        r = tc.build(p)
        assert r.success is False and r.returncode == -1


def test_cmake_build_success_path_mocked(firmware_home, monkeypatch):
    """Mock a working cmake: configure + build succeed and an .elf + .map appear in the build dir."""
    p = fw.new_project(None, "cortex-m4", "cmake")

    monkeypatch.setattr(tc.shutil, "which",
                        lambda tool: "/usr/bin/" + tool
                        if tool in ("cmake", "ninja", "arm-none-eabi-gcc") else None)

    calls: list[list[str]] = []

    def fake_run(cmd, cwd, capture_output, text, timeout, check):  # noqa: ARG001
        calls.append(cmd)
        # The build step (cmake --build) fabricates the artifacts a real toolchain would emit.
        if "--build" in cmd:
            bd = p.build_path
            bd.mkdir(parents=True, exist_ok=True)
            (bd / "firmware.elf").write_bytes(b"\x7fELF fake")
            (bd / "firmware.map").write_text("fake map")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(tc.subprocess, "run", fake_run)

    result = tc.build(p)
    assert result.success is True
    assert result.returncode == 0
    assert result.elf_path is not None and result.elf_path.endswith("firmware.elf")
    assert result.map_path is not None and result.map_path.endswith("firmware.map")
    # configure ran with compile-commands export + the arm toolchain file
    configure = calls[0]
    assert "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" in configure
    assert any(a.startswith("-DCMAKE_TOOLCHAIN_FILE=") for a in configure)


def test_get_toolchain_dispatch():
    assert isinstance(tc.get_toolchain("cmake"), tc.CMakeToolchain)
    assert isinstance(tc.get_toolchain("espidf"), tc.ESPIDFToolchain)
    assert isinstance(tc.get_toolchain("arduino"), tc.ArduinoToolchain)
    with pytest.raises(ValueError):
        tc.get_toolchain("nope")
