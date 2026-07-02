"""Hardware flashing and debug wrappers for the firmware hardware path (T-C4).

probe-rs (MIT/Apache-2.0) and esptool (GPL-2.0+) are SUBPROCESS tools — never linked.
A missing binary is graceful: returns ``FlashResult(success=False, ...)`` (same posture as
:class:`~engine.firmware.toolchains.BuildResult`), never an error envelope or a raise.

esptool is GPL-2.0+; subprocess-only is what keeps it GPL-clean (same posture as QEMU in
``emulator.py``). See ``contracts/licenses.md`` for the ledger rows.

Live-flash DoD items [~]: require a probe + real board — see test_hardware.py for Erik's
run instructions.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

_DEFAULT_TIMEOUT_S = 60.0
_OUTPUT_CAP = 32_000


@dataclass
class FlashResult:
    """Outcome of a flash operation.

    ``success`` is True iff the underlying CLI exited with code 0.
    ``output`` carries combined stdout+stderr (capped).
    ``returncode`` is the raw exit code; ``-1`` means binary absent or timed out.
    ``cmd`` is the command list that was run (for diagnostics).
    ``ms`` is elapsed wall-clock milliseconds.
    """
    success: bool
    output: str = ""
    returncode: int = 0
    cmd: list[str] = field(default_factory=list)
    ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "returncode": self.returncode,
            "cmd": self.cmd,
            "ms": round(self.ms, 2),
        }


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _cap(text: str) -> str:
    return text if len(text) <= _OUTPUT_CAP else (text[:_OUTPUT_CAP] + "\n…[truncated]")


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    """Run *cmd*, return (returncode, combined_output). Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, _cap((proc.stdout or "") + (proc.stderr or ""))
    except FileNotFoundError:
        return -1, f"tool_not_found: executable '{cmd[0]}' not on PATH"
    except subprocess.TimeoutExpired:
        return -1, f"timeout: '{cmd[0]}' exceeded {timeout:.0f}s"


def _not_found(tool: str, cmd: list[str], t0: float) -> FlashResult:
    return FlashResult(
        success=False,
        returncode=-1,
        cmd=cmd,
        output=f"tool_not_found: '{tool}' is not installed / not on PATH",
        ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------------------
# probe-rs flasher (MIT/Apache-2.0)
# ---------------------------------------------------------------------------

class ProbeRsFlasher:
    """Flash an ELF to a target board via probe-rs (MIT/Apache-2.0, subprocess).

    probe-rs must be discoverable on PATH. When absent, returns a graceful
    ``FlashResult(success=False)``.

    Live-flash [~]: ``probe-rs download --chip <chip> <elf>`` — requires a DAP probe
    (e.g. CMSIS-DAP, J-Link) connected to the target and recognised by the host OS.
    """

    BINARY = "probe-rs"

    def flash(self, elf_path: str, chip: str,
              timeout_s: float = _DEFAULT_TIMEOUT_S) -> FlashResult:
        """Flash *elf_path* to a board identified by *chip*.

        Args:
            elf_path: Absolute path to the ``.elf`` firmware image.
            chip: probe-rs chip identifier (e.g. ``"STM32F407VGTx"``,
                  ``"nRF52840_xxAA"``).
            timeout_s: Wall-clock timeout for the flash operation.

        Returns:
            :class:`FlashResult` — ``success=False`` when probe-rs is absent (graceful).

        [~] Live run (Erik): ``probe-rs download --chip <chip> <elf>``
            Requires a DAP probe connected. Confirm chip name against
            ``probe-rs chip list | grep <family>``.
        """
        t0 = time.perf_counter()
        cmd = [self.BINARY, "download", "--chip", chip, elf_path]
        if not shutil.which(self.BINARY):
            return _not_found(self.BINARY, cmd, t0)
        rc, output = _run(cmd, timeout_s)
        return FlashResult(
            success=(rc == 0),
            output=output,
            returncode=rc,
            cmd=cmd,
            ms=(time.perf_counter() - t0) * 1000.0,
        )


# ---------------------------------------------------------------------------
# probe-rs debugger — stateless one-shot subcommands (no persistent session)
# ---------------------------------------------------------------------------

class ProbeRsDebugger:
    """One-shot debug operations via probe-rs (MIT/Apache-2.0, subprocess).

    Each method runs a single probe-rs subcommand and returns. There is no persistent
    debug session — this keeps the API stateless and testable without a real probe.

    Live-debug [~]: a connected DAP probe is required for all methods below.
    """

    BINARY = "probe-rs"

    def read_memory(self, address: int, length: int, chip: str,
                    timeout_s: float = _DEFAULT_TIMEOUT_S) -> bytes:
        """Read *length* bytes from *address* on the target.

        Returns raw bytes on success. Raises ``RuntimeError`` when probe-rs is absent
        or the read fails — callers that want graceful dict output should use the
        :func:`fw_debug_read_mem` MCP handler which wraps this and hex-encodes the result.

        [~] Live run (Erik): ``probe-rs read --chip <chip> <address> <length>``
        """
        if not shutil.which(self.BINARY):
            raise RuntimeError(
                f"tool_not_found: '{self.BINARY}' is not installed / not on PATH"
            )
        cmd = [self.BINARY, "read", "--chip", chip, hex(address), str(length)]
        rc, output = _run(cmd, timeout_s)
        if rc != 0:
            raise RuntimeError(f"probe-rs read failed (rc={rc}): {output}")
        # probe-rs outputs hex bytes; parse them back to raw bytes.
        hex_str = "".join(output.split())
        try:
            return bytes.fromhex(hex_str)
        except ValueError:
            # Output may include non-hex lines (e.g. progress); strip and retry.
            hex_only = "".join(ch for ch in output if ch in "0123456789abcdefABCDEF")
            # Pad to even length
            if len(hex_only) % 2:
                hex_only = hex_only[:-1]
            return bytes.fromhex(hex_only) if hex_only else b""

    def reset(self, chip: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> FlashResult:
        """Reset and run the target.

        [~] Live run (Erik): ``probe-rs reset --chip <chip>``
        """
        t0 = time.perf_counter()
        cmd = [self.BINARY, "reset", "--chip", chip]
        if not shutil.which(self.BINARY):
            return _not_found(self.BINARY, cmd, t0)
        rc, output = _run(cmd, timeout_s)
        return FlashResult(
            success=(rc == 0), output=output, returncode=rc, cmd=cmd,
            ms=(time.perf_counter() - t0) * 1000.0,
        )

    def halt(self, chip: str, timeout_s: float = _DEFAULT_TIMEOUT_S) -> FlashResult:
        """Halt the target core.

        [~] Live run (Erik): ``probe-rs reset --chip <chip> --halt`` (reset into halt)
        """
        t0 = time.perf_counter()
        cmd = [self.BINARY, "reset", "--chip", chip, "--halt"]
        if not shutil.which(self.BINARY):
            return _not_found(self.BINARY, cmd, t0)
        rc, output = _run(cmd, timeout_s)
        return FlashResult(
            success=(rc == 0), output=output, returncode=rc, cmd=cmd,
            ms=(time.perf_counter() - t0) * 1000.0,
        )


# ---------------------------------------------------------------------------
# esptool flasher (GPL-2.0+, subprocess-only)
# ---------------------------------------------------------------------------

class EsptoolFlasher:
    """Flash an ESP32 binary via esptool.py (GPL-2.0+, subprocess-only).

    esptool is GPL-2.0+; calling it as a subprocess (never importing it) keeps the
    project GPL-clean. See ``contracts/licenses.md``.

    Live-flash [~]: requires an ESP32 board connected via USB serial.
    ``esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash 0x0 firmware.bin``
    """

    BINARY = "esptool.py"

    def flash(self, bin_path: str, port: str, baud: int = 460800,
              flash_offset: str = "0x0",
              timeout_s: float = _DEFAULT_TIMEOUT_S) -> FlashResult:
        """Flash *bin_path* to the ESP32 at *port*.

        Args:
            bin_path: Absolute path to the ``.bin`` firmware image.
            port: Serial port device (e.g. ``"/dev/ttyUSB0"``).
            baud: Serial baud rate for flashing (default 460800).
            flash_offset: Flash memory offset in hex (default ``"0x0"``).
            timeout_s: Wall-clock timeout.

        Returns:
            :class:`FlashResult` — ``success=False`` when esptool.py is absent (graceful).

        [~] Live run (Erik):
            ``esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash 0x0 <bin>``
            Board must be in bootloader mode (GPIO0 LOW at reset).
        """
        t0 = time.perf_counter()
        cmd = [
            self.BINARY,
            "--port", port,
            "--baud", str(baud),
            "write_flash",
            flash_offset,
            bin_path,
        ]
        if not shutil.which(self.BINARY):
            return _not_found(self.BINARY, cmd, t0)
        rc, output = _run(cmd, timeout_s)
        return FlashResult(
            success=(rc == 0),
            output=output,
            returncode=rc,
            cmd=cmd,
            ms=(time.perf_counter() - t0) * 1000.0,
        )
