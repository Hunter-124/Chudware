"""Tests for the firmware emulation loop (T-C3): emulator.py + platform_db.py.

Design: the assertion-evaluation logic (``evaluate_assertions``) is a pure function
and is tested directly — no subprocess needed.  The Renode/QEMU subprocess paths are
mocked via ``unittest.mock.patch`` so the suite passes without either binary installed.
Live Renode/QEMU paths are marked ``[~]`` in the DoD and require manual invocation.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.firmware import emulator as emu
from engine.firmware.emulator import (
    AssertionResult,
    QEMUEmulator,
    RenodeEmulator,
    SimResult,
    evaluate_assertions,
    run_simulation,
)
from engine.firmware import platform_db


# ---------------------------------------------------------------------------
# SimResult shape
# ---------------------------------------------------------------------------

def test_simresult_to_dict_shape():
    r = SimResult(
        passed=True,
        uart_log="Hello world\n",
        gpio_states={"LED": 1},
        assertion_results=[AssertionResult("uart:Hello", True, "found in uart_log")],
        ms=42.0,
    )
    d = r.to_dict()
    for key in ("passed", "uart_log", "gpio_states", "assertion_results", "ms"):
        assert key in d, f"SimResult.to_dict() missing '{key}'"
    assert d["passed"] is True
    assert d["gpio_states"] == {"LED": 1}
    assert isinstance(d["assertion_results"], list)
    assert d["assertion_results"][0]["name"] == "uart:Hello"
    assert d["assertion_results"][0]["passed"] is True


def test_simresult_empty_assertions_passes():
    r = SimResult(passed=True)
    assert r.to_dict()["passed"] is True
    assert r.to_dict()["assertion_results"] == []


# ---------------------------------------------------------------------------
# evaluate_assertions — pure function tests (no subprocess)
# ---------------------------------------------------------------------------

def test_uart_assertion_match():
    results = evaluate_assertions("Hello from MCU\n", {}, ["uart:Hello from MCU"])
    assert len(results) == 1
    assert results[0].passed is True


def test_uart_assertion_no_match():
    results = evaluate_assertions("some output", {}, ["uart:BOOT_OK"])
    assert len(results) == 1
    assert results[0].passed is False
    assert "BOOT_OK" in results[0].detail


def test_plain_text_assertion_treated_as_uart():
    # No prefix → implicit uart:
    results = evaluate_assertions("READY\nDONE\n", {}, ["DONE"])
    assert results[0].passed is True


def test_gpio_assertion_match():
    results = evaluate_assertions("", {"LED": 1, "BTN": 0}, ["gpio:LED=1"])
    assert results[0].passed is True


def test_gpio_assertion_mismatch():
    results = evaluate_assertions("", {"LED": 0}, ["gpio:LED=1"])
    assert results[0].passed is False
    assert "LED=0" in results[0].detail


def test_gpio_assertion_missing_pin():
    results = evaluate_assertions("", {}, ["gpio:LED=1"])
    assert results[0].passed is False
    assert "not in gpio_states" in results[0].detail


def test_mixed_assertions_all_pass():
    results = evaluate_assertions("BOOT_OK\nDONE\n", {"LED": 1}, [
        "uart:BOOT_OK", "gpio:LED=1", "DONE"
    ])
    assert all(r.passed for r in results)


def test_mixed_assertions_one_fails():
    results = evaluate_assertions("BOOT_OK\n", {"LED": 0}, [
        "uart:BOOT_OK", "gpio:LED=1"
    ])
    assert results[0].passed is True
    assert results[1].passed is False


def test_empty_assertions_returns_empty():
    results = evaluate_assertions("anything", {"PA5": 1}, [])
    assert results == []


def test_gpio_missing_equals_sign():
    results = evaluate_assertions("", {}, ["gpio:LEDONLY"])
    assert results[0].passed is False
    assert "gpio:PIN=LEVEL" in results[0].detail


def test_gpio_invalid_level():
    results = evaluate_assertions("", {"LED": 1}, ["gpio:LED=high"])
    assert results[0].passed is False


# ---------------------------------------------------------------------------
# platform_db
# ---------------------------------------------------------------------------

def test_platform_db_stm32f4():
    path = platform_db.get_platform_file("cortex-m4")
    # generic fallback always available
    assert path is not None
    assert path.exists()


def test_platform_db_stm32f4_direct():
    path = platform_db.get_platform_file("stm32f4xx")
    assert path is not None
    assert "stm32f4" in path.name


def test_platform_db_esp32():
    path = platform_db.get_platform_file("esp32")
    assert path is not None
    assert "esp32" in path.name


def test_platform_db_unknown_returns_generic():
    path = platform_db.get_platform_file("unknown-mcu-xyz")
    # Falls back to generic cortex_m if it exists
    if path is not None:
        assert path.exists()


def test_platform_db_list():
    platforms = platform_db.list_platforms()
    assert isinstance(platforms, dict)
    assert len(platforms) > 0


# ---------------------------------------------------------------------------
# RenodeEmulator (mocked subprocess)
# ---------------------------------------------------------------------------

def test_renode_absent_graceful():
    """When renode is not on PATH, SimResult.passed=False, not a raise."""
    with patch("engine.firmware.emulator.shutil.which", return_value=None):
        result = RenodeEmulator().run("fake.elf", "fake.repl", ["uart:BOOT_OK"])
    assert isinstance(result, SimResult)
    assert result.passed is False
    assert "emulator_not_found" in result.uart_log
    assert result.ms >= 0


def test_renode_uart_match_via_mock():
    """Mock subprocess returns UART output — assertion passes."""
    mock_proc = MagicMock()
    mock_proc.stdout = "BOOT_OK\nDONE\n"
    mock_proc.stderr = ""
    mock_proc.returncode = 0

    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/renode"), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc), \
         patch("engine.firmware.emulator.tempfile.NamedTemporaryFile") as mock_ntf:
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="f"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_cm.__enter__.return_value.name = "/tmp/test_fake.resc"
        mock_ntf.return_value = mock_cm

        result = RenodeEmulator().run("fake.elf", "fake.repl", ["uart:BOOT_OK", "uart:DONE"])

    assert result.passed is True
    assert "BOOT_OK" in result.uart_log
    assert len(result.assertion_results) == 2
    assert all(a.passed for a in result.assertion_results)


def test_renode_uart_mismatch_via_mock():
    """Assertion fails when UART output doesn't contain expected text."""
    mock_proc = MagicMock()
    mock_proc.stdout = "BOOT_FAILED\n"
    mock_proc.stderr = ""
    mock_proc.returncode = 0

    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/renode"), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc), \
         patch("engine.firmware.emulator.tempfile.NamedTemporaryFile") as mock_ntf:
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="f"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_cm.__enter__.return_value.name = "/tmp/test_fake.resc"
        mock_ntf.return_value = mock_cm

        result = RenodeEmulator().run("fake.elf", "fake.repl", ["uart:BOOT_OK"])

    assert result.passed is False
    assert result.assertion_results[0].passed is False


def test_renode_timeout_graceful():
    """Timeout → SimResult.passed=False with timeout message."""
    import subprocess as _subprocess
    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/renode"), \
         patch("engine.firmware.emulator.subprocess.run",
               side_effect=_subprocess.TimeoutExpired(["renode"], 30)), \
         patch("engine.firmware.emulator.tempfile.NamedTemporaryFile") as mock_ntf:
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="f"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_cm.__enter__.return_value.name = "/tmp/test_fake.resc"
        mock_ntf.return_value = mock_cm

        result = RenodeEmulator().run("fake.elf", "fake.repl", [])

    assert result.passed is False
    assert "timeout" in result.uart_log


# ---------------------------------------------------------------------------
# QEMUEmulator (mocked subprocess)
# ---------------------------------------------------------------------------

def test_qemu_absent_graceful():
    with patch("engine.firmware.emulator.shutil.which", return_value=None):
        result = QEMUEmulator().run("fake.elf")
    assert isinstance(result, SimResult)
    assert result.passed is False
    assert "emulator_not_found" in result.uart_log


def test_qemu_uart_match_via_mock():
    mock_proc = MagicMock()
    mock_proc.stdout = "Hello from QEMU\nDONE\n"
    mock_proc.returncode = 0

    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/qemu-system-arm"), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc):
        result = QEMUEmulator().run("fake.elf", assertions=["uart:Hello from QEMU"])

    assert result.passed is True
    assert "Hello from QEMU" in result.uart_log


def test_qemu_uart_mismatch_via_mock():
    mock_proc = MagicMock()
    mock_proc.stdout = "Error: fault\n"
    mock_proc.returncode = 1

    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/qemu-system-arm"), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc):
        result = QEMUEmulator().run("fake.elf", assertions=["uart:BOOT_OK"])

    assert result.passed is False


def test_qemu_no_assertions_passes():
    """No assertions → passed=True (nothing to fail)."""
    mock_proc = MagicMock()
    mock_proc.stdout = "anything"
    mock_proc.returncode = 0

    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/qemu-system-arm"), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc):
        result = QEMUEmulator().run("fake.elf", assertions=[])

    assert result.passed is True


def test_qemu_timeout_graceful():
    import subprocess as _subprocess
    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/qemu-system-arm"), \
         patch("engine.firmware.emulator.subprocess.run",
               side_effect=_subprocess.TimeoutExpired(["qemu-system-arm"], 30)):
        result = QEMUEmulator().run("fake.elf")
    assert result.passed is False
    assert "timeout" in result.uart_log


# ---------------------------------------------------------------------------
# run_simulation — integration dispatcher
# ---------------------------------------------------------------------------

def test_run_simulation_neither_emulator():
    """Both emulators absent → graceful SimResult."""
    with patch("engine.firmware.emulator.shutil.which", return_value=None):
        result = run_simulation("fake.elf", "cortex-m4", ["uart:BOOT_OK"])
    assert isinstance(result, SimResult)
    assert result.passed is False
    assert "emulator_not_found" in result.uart_log


def test_run_simulation_prefers_renode_when_platform_exists():
    mock_proc = MagicMock()
    mock_proc.stdout = "BOOT_OK\n"
    mock_proc.stderr = ""
    mock_proc.returncode = 0

    def mock_which(binary):
        return "/usr/bin/" + binary if binary == "renode" else None

    with patch("engine.firmware.emulator.shutil.which", side_effect=mock_which), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc), \
         patch("engine.firmware.emulator.tempfile.NamedTemporaryFile") as mock_ntf:
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="f"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_cm.__enter__.return_value.name = "/tmp/test_fake.resc"
        mock_ntf.return_value = mock_cm

        result = run_simulation("fake.elf", "cortex-m4", ["uart:BOOT_OK"])

    assert result.passed is True


def test_run_simulation_falls_back_to_qemu():
    """Renode absent but QEMU present → uses QEMU."""
    mock_proc = MagicMock()
    mock_proc.stdout = "BOOT_OK\n"
    mock_proc.returncode = 0

    def mock_which(binary):
        return "/usr/bin/" + binary if binary == "qemu-system-arm" else None

    with patch("engine.firmware.emulator.shutil.which", side_effect=mock_which), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc):
        result = run_simulation("fake.elf", "cortex-m4", ["uart:BOOT_OK"])

    assert result.passed is True
