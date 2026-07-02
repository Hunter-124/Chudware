"""Tests for the firmware hardware path (T-C4): hardware.py + lsp.py + mcp_tools.py handlers.

Design: all subprocess calls are mocked via ``unittest.mock.patch`` so the suite passes
without probe-rs, esptool.py, or clangd installed. Live-flash DoD items are marked [~] in
comments; they require a probe/board and are verified manually by Erik.

Mock strategy (mirrors test_emulator.py):
- ``shutil.which`` is patched to control tool-present/absent branching.
- ``subprocess.run`` is patched to inject return codes and output.
- ``subprocess.Popen`` is patched for the clangd manager tests.
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from engine.firmware.hardware import (
    EsptoolFlasher,
    FlashResult,
    ProbeRsDebugger,
    ProbeRsFlasher,
)
from engine.firmware.lsp import ClangdManager
from engine.firmware import mcp_tools as fw


# ---------------------------------------------------------------------------
# FlashResult shape
# ---------------------------------------------------------------------------

def test_flashresult_to_dict_shape():
    r = FlashResult(
        success=True, output="Flashing done", returncode=0,
        cmd=["probe-rs", "download"], ms=123.4,
    )
    d = r.to_dict()
    for key in ("success", "output", "returncode", "cmd", "ms"):
        assert key in d, f"FlashResult.to_dict() missing '{key}'"
    assert d["success"] is True
    assert d["returncode"] == 0
    assert d["ms"] == 123.4


def test_flashresult_defaults():
    r = FlashResult(success=False)
    assert r.output == ""
    assert r.returncode == 0
    assert r.cmd == []
    assert r.ms == 0.0


# ---------------------------------------------------------------------------
# ProbeRsFlasher — binary absent (graceful degradation)
# ---------------------------------------------------------------------------

def test_probe_rs_flasher_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = ProbeRsFlasher().flash("/fw/blink.elf", "STM32F407VGTx")
    assert result.success is False
    assert result.returncode == -1
    assert "probe-rs" in result.output
    assert "not on PATH" in result.output


# ---------------------------------------------------------------------------
# ProbeRsFlasher — happy path (zero exit code)
# ---------------------------------------------------------------------------

def test_probe_rs_flasher_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Erasing sectors done\nFlashing done\n"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = ProbeRsFlasher().flash("/fw/blink.elf", "STM32F407VGTx")
    assert result.success is True
    assert result.returncode == 0
    assert "probe-rs" in result.cmd[0]
    assert "--chip" in result.cmd
    assert "STM32F407VGTx" in result.cmd
    assert "/fw/blink.elf" in result.cmd


# ---------------------------------------------------------------------------
# ProbeRsFlasher — nonzero exit code → success=False
# ---------------------------------------------------------------------------

def test_probe_rs_flasher_nonzero_exit():
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "Error: failed to connect to probe"
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = ProbeRsFlasher().flash("/fw/blink.elf", "STM32F407VGTx")
    assert result.success is False
    assert result.returncode == 1
    assert "failed to connect" in result.output


# ---------------------------------------------------------------------------
# EsptoolFlasher — binary absent (graceful degradation)
# ---------------------------------------------------------------------------

def test_esptool_flasher_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = EsptoolFlasher().flash("/fw/firmware.bin", "/dev/ttyUSB0")
    assert result.success is False
    assert result.returncode == -1
    assert "esptool.py" in result.output
    assert "not on PATH" in result.output


# ---------------------------------------------------------------------------
# EsptoolFlasher — happy path (zero exit code)
# ---------------------------------------------------------------------------

def test_esptool_flasher_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Wrote 262144 bytes"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/esptool.py"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = EsptoolFlasher().flash("/fw/firmware.bin", "/dev/ttyUSB0", baud=460800)
    assert result.success is True
    assert result.returncode == 0
    assert "--port" in result.cmd
    assert "/dev/ttyUSB0" in result.cmd
    assert "--baud" in result.cmd
    assert "460800" in result.cmd
    assert "write_flash" in result.cmd


# ---------------------------------------------------------------------------
# EsptoolFlasher — nonzero exit
# ---------------------------------------------------------------------------

def test_esptool_flasher_failure():
    mock_proc = MagicMock()
    mock_proc.returncode = 2
    mock_proc.stdout = ""
    mock_proc.stderr = "A fatal error occurred: Failed to connect to ESP32"
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/esptool.py"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = EsptoolFlasher().flash("/fw/firmware.bin", "/dev/ttyUSB0")
    assert result.success is False
    assert result.returncode == 2


# ---------------------------------------------------------------------------
# ProbeRsDebugger.read_memory — absent
# ---------------------------------------------------------------------------

def test_probe_rs_debugger_read_memory_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="not on PATH"):
            ProbeRsDebugger().read_memory(0x20000000, 4, "STM32F407VGTx")


# ---------------------------------------------------------------------------
# ProbeRsDebugger.read_memory — success (hex bytes returned)
# ---------------------------------------------------------------------------

def test_probe_rs_debugger_read_memory_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "deadbeef"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        data = ProbeRsDebugger().read_memory(0x20000000, 4, "STM32F407VGTx")
    assert isinstance(data, bytes)
    assert data == bytes.fromhex("deadbeef")


# ---------------------------------------------------------------------------
# ProbeRsDebugger.reset / halt — absent
# ---------------------------------------------------------------------------

def test_probe_rs_debugger_reset_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = ProbeRsDebugger().reset("STM32F407VGTx")
    assert result.success is False
    assert "not on PATH" in result.output


def test_probe_rs_debugger_halt_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = ProbeRsDebugger().halt("STM32F407VGTx")
    assert result.success is False
    assert "--halt" in result.cmd


# ---------------------------------------------------------------------------
# ClangdManager — absent
# ---------------------------------------------------------------------------

def test_clangd_manager_start_not_found():
    with patch("engine.firmware.lsp.shutil.which", return_value=None):
        proc = ClangdManager().start("/project")
    assert proc is None


def test_clangd_get_diagnostics_not_found():
    with patch("engine.firmware.lsp.shutil.which", return_value=None):
        diags = ClangdManager().get_diagnostics("/project/src/main.c")
    assert len(diags) == 1
    assert "clangd_not_found" in diags[0].get("reason", "")


# ---------------------------------------------------------------------------
# ClangdManager — start cmd includes --query-driver
# ---------------------------------------------------------------------------

def test_clangd_manager_start_uses_query_driver():
    """Verify start() passes --query-driver for arm-none-eabi."""
    mock_popen = MagicMock()
    with patch("engine.firmware.lsp.shutil.which", return_value="/usr/bin/clangd"), \
         patch("engine.firmware.lsp.subprocess.Popen", return_value=mock_popen) as mock_cls:
        result = ClangdManager().start("/project", build_dir="/project/build")
    assert result is mock_popen
    args = mock_cls.call_args[0][0]  # first positional arg is the cmd list
    assert any("--query-driver" in a for a in args)
    assert any("arm-none-eabi" in a for a in args)
    assert any("--compile-commands-dir" in a for a in args)


# ---------------------------------------------------------------------------
# MCP handler: fw_flash (dict in → dict out)
# ---------------------------------------------------------------------------

def test_fw_flash_missing_elf_path():
    result = fw.fw_flash({})
    assert "code" in result  # error envelope
    assert result["code"] == "invalid_request"


def test_fw_flash_missing_chip_for_probe_rs():
    result = fw.fw_flash({"elf_path": "/fw/blink.elf"})
    assert result["code"] == "invalid_request"
    assert "chip" in result["message"]


def test_fw_flash_probe_rs_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = fw.fw_flash({"elf_path": "/fw/blink.elf", "chip": "STM32F407VGTx"})
    assert result["success"] is False
    assert "not on PATH" in result["output"]


def test_fw_flash_probe_rs_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Flashing done"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = fw.fw_flash({"elf_path": "/fw/blink.elf", "chip": "STM32F407VGTx"})
    assert result["success"] is True


def test_fw_flash_esptool_routed_by_port_and_bin():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Wrote bytes"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/esptool.py"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = fw.fw_flash({
            "elf_path": "/fw/firmware.bin",
            "chip": "esp32",
            "port": "/dev/ttyUSB0",
        })
    assert result["success"] is True


# ---------------------------------------------------------------------------
# MCP handler: fw_debug_start
# ---------------------------------------------------------------------------

def test_fw_debug_start_missing_chip():
    result = fw.fw_debug_start({})
    assert result["code"] == "invalid_request"


def test_fw_debug_start_probe_rs_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = fw.fw_debug_start({"chip": "STM32F407VGTx"})
    assert result["success"] is False


def test_fw_debug_start_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Core was reset and is now halted"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = fw.fw_debug_start({"chip": "STM32F407VGTx"})
    assert result["success"] is True


# ---------------------------------------------------------------------------
# MCP handler: fw_debug_halt
# ---------------------------------------------------------------------------

def test_fw_debug_halt_missing_chip():
    result = fw.fw_debug_halt({})
    assert result["code"] == "invalid_request"


def test_fw_debug_halt_probe_rs_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = fw.fw_debug_halt({"chip": "STM32F407VGTx"})
    assert result["success"] is False
    assert "--halt" in result["cmd"]


def test_fw_debug_halt_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Halted"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = fw.fw_debug_halt({"chip": "STM32F407VGTx"})
    assert result["success"] is True


# ---------------------------------------------------------------------------
# MCP handler: fw_debug_read_mem
# ---------------------------------------------------------------------------

def test_fw_debug_read_mem_missing_params():
    result = fw.fw_debug_read_mem({})
    assert result["code"] == "invalid_request"

    result = fw.fw_debug_read_mem({"chip": "STM32F407VGTx"})
    assert result["code"] == "invalid_request"


def test_fw_debug_read_mem_probe_rs_not_found():
    with patch("engine.firmware.hardware.shutil.which", return_value=None):
        result = fw.fw_debug_read_mem({
            "chip": "STM32F407VGTx",
            "address": "0x20000000",
            "length": 4,
        })
    assert result["success"] is False
    assert "not on PATH" in result["reason"]


def test_fw_debug_read_mem_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "deadbeef"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = fw.fw_debug_read_mem({
            "chip": "STM32F407VGTx",
            "address": "0x20000000",
            "length": 4,
        })
    assert result["success"] is True
    assert result["data_hex"] == "deadbeef"
    assert result["address"] == "0x20000000"
    assert result["length"] == 4


def test_fw_debug_read_mem_invalid_address():
    result = fw.fw_debug_read_mem({
        "chip": "STM32F407VGTx",
        "address": "not_a_number",
        "length": 4,
    })
    assert result["code"] == "invalid_request"


def test_fw_debug_read_mem_hex_address():
    """address as hex string is parsed correctly."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "aabbccdd"
    mock_proc.stderr = ""
    with patch("engine.firmware.hardware.shutil.which", return_value="/usr/bin/probe-rs"), \
         patch("engine.firmware.hardware.subprocess.run", return_value=mock_proc):
        result = fw.fw_debug_read_mem({
            "chip": "nRF52840_xxAA",
            "address": "0x20000010",
            "length": 4,
        })
    assert result["success"] is True
    assert result["address"] == hex(0x20000010)
