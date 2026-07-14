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

        # T-FIN2: use a family with a real UART-capable platform (the generic cortex-m
        # platform has no UART model, so a uart assertion there now falls back/refuses).
        result = run_simulation("fake.elf", "stm32f407", ["uart:BOOT_OK"])

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


# ---------------------------------------------------------------------------
# T-FIN1: GPIO trace parsing (canned emulator output — no subprocess)
# ---------------------------------------------------------------------------

CANNED_RENODE = """\
12:34:56.0000 [INFO] machine-0: Machine started.
BOOT_OK
AICAD_GPIO_EVT PA5=1
some unrelated renode chatter
AICAD_GPIO_EVT PA5=0
AICAD_GPIO_EVT PB3=1
AICAD_GPIO_ODR_BEGIN A
0x00000020
AICAD_GPIO_ODR_END A
AICAD_GPIO_ODR_BEGIN B
0x00000008
AICAD_GPIO_ODR_END B
"""

CANNED_QEMU = """\
Hello from QEMU
AICAD_GPIO_EVT PA5=1 t=12ms
AICAD_GPIO_EVT PA5=0 t=25.5ms
AICAD_GPIO_EVT LED=1 t=1000us
DONE
"""


def test_parse_gpio_events_renode_canned():
    events = emu.parse_gpio_events(CANNED_RENODE)
    assert [(e["pin"], e["level"]) for e in events] == [("PA5", 1), ("PA5", 0), ("PB3", 1)]
    assert all(e["t_ms"] is None for e in events)  # Renode hook prints carry no timestamp
    assert events[0]["seq"] < events[1]["seq"] < events[2]["seq"]


def test_parse_gpio_events_qemu_timestamps_and_units():
    events = emu.parse_gpio_events(CANNED_QEMU)
    assert len(events) == 3
    assert events[0]["t_ms"] == 12.0
    assert events[1]["t_ms"] == 25.5
    assert events[2] == {"type": "gpio", "seq": 3, "t_ms": 1.0, "pin": "LED", "level": 1}


def test_parse_renode_gpio_odr_expands_bits():
    states = emu.parse_renode_gpio_odr(CANNED_RENODE)
    assert states["PA5"] == 1          # 0x20 = bit 5
    assert states["PA0"] == 0
    assert states["PB3"] == 1          # 0x08 = bit 3
    assert states["PB4"] == 0
    assert len(states) == 32           # two full 16-pin ports


def test_parse_renode_gpio_odr_unpaired_markers_ignored():
    text = "AICAD_GPIO_ODR_BEGIN A\nno hex here\nAICAD_GPIO_ODR_END B\n"
    assert emu.parse_renode_gpio_odr(text) == {}


def test_derive_gpio_states_odr_wins_over_last_event():
    events = emu.parse_gpio_events("AICAD_GPIO_EVT PA5=0\nAICAD_GPIO_EVT LED=1\n")
    final = emu.derive_gpio_states(events, {"PA5": 1})
    assert final["PA5"] == 1   # ODR snapshot (read after the run) overrides the event
    assert final["LED"] == 1   # event-only pin kept


def test_pins_from_assertions():
    pins = emu._pins_from_assertions(
        ["gpio:PA5=1", "gpio:PB12=0@>10ms", "gpio:LED=1", "uart:x", "gpio:PA5=0"])
    assert pins == [("A", 5), ("B", 12)]   # dedup, non-STM32 names skipped


# ---------------------------------------------------------------------------
# T-FIN1: timeline assertion evaluation
# ---------------------------------------------------------------------------

def test_timeline_assertion_after():
    events = emu.parse_gpio_events(CANNED_QEMU)
    r = evaluate_assertions("", {}, ["gpio:PA5=0@>20ms"], gpio_events=events)
    assert r[0].passed is True
    r = evaluate_assertions("", {}, ["gpio:PA5=1@>20ms"], gpio_events=events)
    assert r[0].passed is False


def test_timeline_assertion_before_and_at():
    events = emu.parse_gpio_events(CANNED_QEMU)
    assert evaluate_assertions("", {}, ["gpio:PA5=1@<20ms"], gpio_events=events)[0].passed
    assert evaluate_assertions("", {}, ["gpio:PA5=1@12ms"], gpio_events=events)[0].passed
    assert not evaluate_assertions("", {}, ["gpio:PA5=1@13ms"], gpio_events=events)[0].passed


def test_timeline_assertion_no_timestamped_events():
    events = emu.parse_gpio_events("AICAD_GPIO_EVT PA5=1\n")  # no t=
    r = evaluate_assertions("", {"PA5": 1}, ["gpio:PA5=1@>0ms"], gpio_events=events)
    assert r[0].passed is False
    assert "no time-stamped events" in r[0].detail


def test_final_state_assertion_backward_compatible_signature():
    # old 3-arg call still works
    r = evaluate_assertions("", {"PA5": 1}, ["gpio:PA5=1"])
    assert r[0].passed is True


# ---------------------------------------------------------------------------
# T-FIN1: .resc generation gains hooks + ODR reads
# ---------------------------------------------------------------------------

def test_build_resc_gpio_hooks_and_odr(tmp_path):
    repl = tmp_path / "p.repl"
    repl.write_text("usart1: UART.STM32_UART @ sysbus\n"
                    "gpioPortA: GPIOPort.STM32F4GPIOPort @ sysbus <0x40020000, +0x400>\n",
                    encoding="utf-8")
    script = emu._build_resc("fw.elf", str(repl), gpio_pins=[("A", 5), ("B", 3)])
    assert "AddStateChangedHook 5" in script
    assert "AICAD_GPIO_EVT PA5" in script
    # port B not declared in the platform → no hook, no ODR read
    assert "gpioPortB" not in script
    assert 'AICAD_GPIO_ODR_BEGIN A' in script
    assert "sysbus ReadDoubleWord 0x40020014" in script
    # ODR read comes after the run sleep
    assert script.index("sleep") < script.index("AICAD_GPIO_ODR_BEGIN")


def test_build_resc_no_gpio_pins_unchanged(tmp_path):
    repl = tmp_path / "p.repl"
    repl.write_text("gpioPortA: GPIOPort.STM32F4GPIOPort @ sysbus\n", encoding="utf-8")
    script = emu._build_resc("fw.elf", str(repl))
    assert "AddStateChangedHook" not in script
    assert "AICAD_GPIO_ODR" not in script


def test_stm32f4_platform_declares_gpio_ports():
    path = platform_db.get_platform_file("stm32f4xx")
    text = path.read_text(encoding="utf-8")
    for port in "ABCDE":
        assert f"gpioPort{port}" in text


# ---------------------------------------------------------------------------
# T-FIN1: end-to-end (mocked subprocess) — gpio assertion passes on canned output
# ---------------------------------------------------------------------------

def _renode_run_with_output(stdout: str, elf_path: str, assertions: list[str]) -> SimResult:
    mock_proc = MagicMock()
    mock_proc.stdout = stdout
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
        return RenodeEmulator().run(elf_path, "fake.repl", assertions)


def test_renode_gpio_assertion_passes_from_canned_output(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    result = _renode_run_with_output(CANNED_RENODE, str(elf),
                                     ["gpio:PA5=1", "gpio:PB3=1", "uart:BOOT_OK"])
    assert result.passed is True
    assert result.gpio_states["PA5"] == 1
    assert len(result.gpio_events) == 3


def test_renode_gpio_assertion_fails_on_wrong_level(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    result = _renode_run_with_output(CANNED_RENODE, str(elf), ["gpio:PA5=0"])
    assert result.passed is False
    assert "PA5=1" in result.assertion_results[0].detail


def test_renode_trace_artifact_written(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    result = _renode_run_with_output(CANNED_RENODE, str(elf), ["gpio:PA5=1"])
    assert result.trace_path is not None
    trace = json.loads(Path(result.trace_path).read_text(encoding="utf-8"))
    assert trace["schema"] == "aicad.firmware.trace/1"
    assert trace["source"] == "renode"
    assert trace["gpio_final"]["PA5"] == 1
    types = {e["type"] for e in trace["events"]}
    assert types == {"gpio", "uart"}
    seqs = [e["seq"] for e in trace["events"]]
    assert seqs == sorted(seqs)
    # marker lines excluded from uart events
    assert not any("AICAD_GPIO" in e.get("text", "") for e in trace["events"])


def test_qemu_semihosting_gpio_and_trace(tmp_path):
    elf = tmp_path / "fw.elf"
    elf.write_bytes(b"\x7fELF")
    mock_proc = MagicMock()
    mock_proc.stdout = CANNED_QEMU
    mock_proc.returncode = 0
    with patch("engine.firmware.emulator.shutil.which",
               return_value="/usr/bin/qemu-system-arm"), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc):
        result = QEMUEmulator().run(str(elf), assertions=[
            "gpio:PA5=0", "gpio:PA5=1@<20ms", "uart:DONE"])
    assert result.passed is True
    assert result.gpio_states == {"PA5": 0, "LED": 1}
    trace = json.loads(Path(result.trace_path).read_text(encoding="utf-8"))
    assert trace["source"] == "qemu"
    assert any(e["type"] == "gpio" and e["t_ms"] == 12.0 for e in trace["events"])


def test_trace_write_failure_is_graceful():
    # Unwritable directory → trace_path None, never a raise
    path = emu.write_trace_artifact("/nonexistent-dir-xyz/fw.elf", "qemu", "x", [], {})
    assert path is None


def test_trace_template_header_exists():
    hdr = Path(emu.__file__).parent / "templates" / "cmake_arm" / "include" / "aicad_trace.h"
    assert hdr.exists()
    assert "AICAD_GPIO_TRACE" in hdr.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T-FIN1: live capability-gated smoke (skips cleanly without the binaries)
# ---------------------------------------------------------------------------

import shutil as _shutil


@pytest.mark.skipif(_shutil.which("renode") is None, reason="renode not installed")
def test_live_renode_resc_accepted(tmp_path):
    """[~] live: the generated .resc (with gpio hooks) must not crash Renode outright."""
    elf = tmp_path / "empty.elf"
    elf.write_bytes(b"")
    platform = platform_db.get_platform_file("stm32f4xx")
    result = RenodeEmulator().run(str(elf), str(platform), ["gpio:PA5=1"], timeout_s=15)
    assert isinstance(result, SimResult)  # graceful either way; no raise


@pytest.mark.skipif(_shutil.which("qemu-system-arm") is None,
                    reason="qemu-system-arm not installed")
def test_live_qemu_graceful_on_bogus_elf(tmp_path):
    elf = tmp_path / "empty.elf"
    elf.write_bytes(b"")
    result = QEMUEmulator().run(str(elf), timeout_s=10, assertions=["gpio:PA5=1"])
    assert isinstance(result, SimResult)
    assert result.passed is False  # bogus ELF can't satisfy the assertion


# ---------------------------------------------------------------------------
# T-FIN2: platform pack — structure tests (renode absent on this machine, so
# canned-output + structure tests carry the load; live smoke below is gated)
# ---------------------------------------------------------------------------

_PLATFORM_DIR = Path(platform_db.__file__).parent / "templates" / "renode_platforms"

# family → peripherals its .repl must declare (name substrings)
_EXPECTED_DECLS = {
    "stm32f4": ["usart1", "usart2", "gpioPortA", "gpioPortE", "i2c1", "spi1", "timer2", "nvic", "cpu"],
    "stm32f1": ["usart1", "usart5", "gpioPortA", "gpioPortG", "i2c1", "timer2", "nvic", "cpu"],
    "nrf52":   ["uart0", "gpio0", "gpiote", "twi0", "spi2", "timer0", "nvic", "cpu"],
    "rp2040":  ["uart0", "uart1", "nvic", "cpu", "flash", "sram"],
    "esp32":   ["cpu0", "flash", "sram"],
    "cortex-m": ["nvic", "cpu", "flash", "sram"],
}

import re  # noqa: E402

# .repl top-level declaration / continuation grammar sanity (comment, blank, decl, indented attr)
_REPL_LINE_RE = re.compile(
    r"^($|//|\w[\w]*\s*:\s*\S|sysbus:$|[a-zA-Z_]\w*:$|\s+\S)")


@pytest.mark.parametrize("family", sorted(platform_db.PLATFORM_CAPS))
def test_platform_file_exists_and_structure(family):
    caps = platform_db.PLATFORM_CAPS[family]
    path = _PLATFORM_DIR / caps["file"]
    assert path.exists(), f"missing platform file for {family}: {caps['file']}"
    text = path.read_text(encoding="utf-8")
    assert "\t" not in text, ".repl files must be space-indented"
    assert "ApplySVD @http" not in text, "no network fetches at machine-load time"
    for line in text.splitlines():
        assert _REPL_LINE_RE.match(line), f"{caps['file']}: suspicious .repl line: {line!r}"


@pytest.mark.parametrize("family,decls", sorted(_EXPECTED_DECLS.items()))
def test_platform_declares_expected_peripherals(family, decls):
    path = _PLATFORM_DIR / platform_db.PLATFORM_CAPS[family]["file"]
    text = path.read_text(encoding="utf-8")
    for name in decls:
        assert re.search(rf"^{name}\s*:", text, re.MULTILINE), \
            f"{family}: platform must declare '{name}'"


def test_capability_matrix_shape_and_honesty():
    matrix = platform_db.capability_matrix()
    assert set(matrix) == {"stm32f4", "stm32f1", "nrf52", "rp2040", "esp32", "cortex-m"}
    for family, caps in matrix.items():
        for key in ("file", "emulation", "uart", "gpio", "gpio_observe",
                    "adc", "i2c", "spi", "timers", "notes"):
            assert key in caps, f"{family} missing '{key}'"
        # honesty: no family claims an ADC — upstream Renode ships no ADC model here
        assert caps["adc"] is False
    assert matrix["esp32"]["emulation"] == "none"
    assert matrix["rp2040"]["emulation"] == "renode-minimal"
    assert matrix["nrf52"]["gpio_observe"] is False   # observation wiring is STM32-only
    assert matrix["stm32f4"]["gpio_observe"] is True


@pytest.mark.parametrize("raw,family", [
    ("STM32F407VG", "stm32f4"), ("stm32f103c8", "stm32f1"),
    ("nRF52840_xxAA", "nrf52"), ("RP2040", "rp2040"), ("pico", "rp2040"),
    ("esp32-wroom", "esp32"), ("cortex-m4", "cortex-m"), ("weird-mcu", "cortex-m"),
])
def test_canonical_family(raw, family):
    assert platform_db.canonical_family(raw) == family


def test_gpio_regmap_stm32f1_differs_from_f4():
    f4 = platform_db.gpio_regmap_for("stm32f4")
    f1 = platform_db.gpio_regmap_for("stm32f103")
    assert f4 == {"base": 0x40020000, "stride": 0x400, "odr_offset": 0x14}
    assert f1 == {"base": 0x40010800, "stride": 0x400, "odr_offset": 0x0C}
    assert platform_db.gpio_regmap_for("nrf52") is None


def test_build_resc_uses_first_uart_name(tmp_path):
    repl = tmp_path / "p.repl"
    repl.write_text("uart0: UART.NRF52840_UART @ sysbus 0x40002000\n", encoding="utf-8")
    script = emu._build_resc("fw.elf", str(repl))
    assert "showAnalyzer sysbus.uart0" in script
    assert "usart1" not in script


def test_build_resc_stm32f1_odr_addresses(tmp_path):
    repl = tmp_path / "p.repl"
    repl.write_text("usart1: UART.STM32_UART @ sysbus\n"
                    "gpioPortA: GPIOPort.STM32F1GPIOPort @ sysbus <0x40010800, +0x400>\n",
                    encoding="utf-8")
    script = emu._build_resc("fw.elf", str(repl), gpio_pins=[("A", 5)],
                             gpio_regmap=platform_db.gpio_regmap_for("stm32f1"))
    assert "readdoubleword 0x4001080c" in script.lower()


# ---------------------------------------------------------------------------
# T-FIN2: honest refusals in run_simulation
# ---------------------------------------------------------------------------

def test_run_simulation_esp32_refuses_honestly():
    # Even with renode "installed", esp32 has emulation="none" — refuse before dispatch.
    with patch("engine.firmware.emulator.shutil.which", return_value="/usr/bin/renode"), \
         patch("engine.firmware.emulator.subprocess.run") as mock_run:
        result = run_simulation("fake.elf", "esp32", ["uart:BOOT_OK", "gpio:PA5=1"])
    mock_run.assert_not_called()
    assert result.passed is False
    assert "platform_capability" in result.uart_log
    assert "build + flash only" in result.uart_log
    assert len(result.assertion_results) == 2
    assert all(not a.passed for a in result.assertion_results)
    assert "unsupported on platform" in result.assertion_results[0].detail


def test_run_simulation_gpio_on_nrf52_refuses_without_qemu():
    def which(binary):
        return "/usr/bin/renode" if binary == "renode" else None
    with patch("engine.firmware.emulator.shutil.which", side_effect=which), \
         patch("engine.firmware.emulator.subprocess.run") as mock_run:
        result = run_simulation("fake.elf", "nrf52840", ["gpio:PA5=1", "uart:BOOT_OK"])
    mock_run.assert_not_called()
    assert result.passed is False
    assert "gpio observation" in result.uart_log
    by_name = {a.name: a for a in result.assertion_results}
    assert "unsupported on platform" in by_name["gpio:PA5=1"].detail
    assert "not evaluated" in by_name["uart:BOOT_OK"].detail


def test_run_simulation_gpio_on_nrf52_falls_back_to_qemu():
    mock_proc = MagicMock()
    mock_proc.stdout = "AICAD_GPIO_EVT PA5=1\n"
    mock_proc.returncode = 0
    with patch("engine.firmware.emulator.shutil.which",
               side_effect=lambda b: "/usr/bin/" + b), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc):
        result = run_simulation("fake.elf", "nrf52840", ["gpio:PA5=1"])
    assert result.passed is True   # semihosting shim path handled it


def test_run_simulation_uart_on_generic_cortex_m_refuses_without_qemu():
    def which(binary):
        return "/usr/bin/renode" if binary == "renode" else None
    with patch("engine.firmware.emulator.shutil.which", side_effect=which), \
         patch("engine.firmware.emulator.subprocess.run") as mock_run:
        result = run_simulation("fake.elf", "cortex-m4", ["uart:BOOT_OK"])
    mock_run.assert_not_called()
    assert result.passed is False
    assert "UART model" in result.uart_log


def test_run_simulation_no_assertions_still_runs_renode():
    mock_proc = MagicMock()
    mock_proc.stdout = "Machine started\n"
    mock_proc.stderr = ""
    mock_proc.returncode = 0
    def which(binary):
        return "/usr/bin/renode" if binary == "renode" else None
    with patch("engine.firmware.emulator.shutil.which", side_effect=which), \
         patch("engine.firmware.emulator.subprocess.run", return_value=mock_proc), \
         patch("engine.firmware.emulator.tempfile.NamedTemporaryFile") as mock_ntf:
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(name="f"))
        mock_cm.__exit__ = MagicMock(return_value=False)
        mock_cm.__enter__.return_value.name = "/tmp/test_fake.resc"
        mock_ntf.return_value = mock_cm
        result = run_simulation("fake.elf", "cortex-m4", [])
    assert result.passed is True   # no assertions → boot smoke is allowed on any platform


# ---------------------------------------------------------------------------
# T-FIN2: per-platform live boot smoke — skips cleanly when renode is absent
# (it IS absent on this machine; .repl syntax historically only fails live, so
# these are the real gate on a provisioned box)
# ---------------------------------------------------------------------------

_LOAD_FAIL_MARKERS = ("Could not create machine", "ParsingError", "parsing error",
                      "Could not find suitable constructor",
                      "expected attribute list end")


@pytest.mark.skipif(_shutil.which("renode") is None, reason="renode not installed")
@pytest.mark.parametrize("family", ["stm32f4", "stm32f103", "nrf52840", "rp2040"])
def test_live_platform_boots(family, tmp_path):
    """[~] live: each curated platform must load in Renode without a .repl error."""
    elf = tmp_path / "empty.elf"
    elf.write_bytes(b"")
    platform = platform_db.get_platform_file(family)
    assert platform is not None
    result = RenodeEmulator().run(str(elf), str(platform), [], timeout_s=20)
    assert isinstance(result, SimResult)
    for marker in _LOAD_FAIL_MARKERS:
        assert marker not in result.uart_log, \
            f"{family}: platform failed to load live: ...{marker}..."


@pytest.mark.skipif(_shutil.which("renode") is None or
                    _shutil.which("arm-none-eabi-gcc") is None,
                    reason="renode + arm-none-eabi-gcc required")
def test_live_stm32f4_hello_uart(tmp_path):
    """[~] live: bare-metal USART1 hello prints over Renode's console analyzer."""
    import subprocess as sp
    src = tmp_path / "hello.c"
    src.write_text(
        "typedef unsigned int u32;\n"
        "static void send(const char *s){volatile u32 *dr=(u32*)0x40011004;"
        "while(*s){*dr=(u32)*s++;}}\n"
        "void _start(void){*(volatile u32*)0x4001100C=(1u<<13)|(1u<<3);"  # USART1 CR1: UE|TE (Renode 1.16.1 enforces TE)
        "send(\"HELLO_UART\\n\");for(;;){}}\n"
        "__attribute__((section(\".vtab\"),used)) static void(*v[2])(void)="
        "{(void(*)(void))0x20004000,_start};\n",
        encoding="utf-8")
    elf = tmp_path / "hello.elf"
    ld = tmp_path / "link.ld"
    ld.write_text("SECTIONS{ .text 0x08000000 : { KEEP(*(.vtab)) *(.text*) *(.rodata*) } }\n",
                  encoding="utf-8")
    sp.run(["arm-none-eabi-gcc", "-mcpu=cortex-m4", "-mthumb", "-nostdlib",
            "-ffreestanding", "-T", str(ld), "-o", str(elf), str(src)], check=True)
    platform = platform_db.get_platform_file("stm32f4")
    result = RenodeEmulator().run(str(elf), str(platform), ["uart:HELLO_UART"], timeout_s=25)
    assert result.passed, result.uart_log
