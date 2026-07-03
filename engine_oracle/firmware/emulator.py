"""Renode and QEMU emulator wrappers for the firmware emulation loop (T-C3).

Both :class:`RenodeEmulator` and :class:`QEMUEmulator` follow the same posture as
:mod:`engine.firmware.toolchains`: subprocess-only, never imported or linked; a missing
binary is graceful (``passed=False``, ``uart_log`` carries the reason), never a raise.

Renode is MIT-licensed (subprocess, never vendored). QEMU is GPL-2.0+ (subprocess only).
Platform files bundled in ``templates/renode_platforms/`` are also MIT; see
``contracts/licenses.md`` for the ledger row.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_DEFAULT_TIMEOUT_S = 30.0
_UART_CAP = 32_000  # keep captured UART log bounded


@dataclass
class AssertionResult:
    """Outcome of a single assertion check."""
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class SimResult:
    """See ``contracts/firmware_rpc.md`` §4.

    ``passed`` is True iff every assertion passed.  ``uart_log`` carries captured
    UART/semihosting text; ``gpio_states`` is a pin-name → level mapping at end of run.
    When an emulator binary is absent ``passed=False`` and ``uart_log`` explains why —
    this is the "graceful degradation" equivalent of :class:`BuildResult` (``success=False``).
    """
    passed: bool
    uart_log: str = ""
    gpio_states: dict[str, int] = field(default_factory=dict)
    assertion_results: list[AssertionResult] = field(default_factory=list)
    ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "uart_log": self.uart_log,
            "gpio_states": self.gpio_states,
            "assertion_results": [a.to_dict() for a in self.assertion_results],
            "ms": round(self.ms, 2),
        }


# ---------------------------------------------------------------------------
# Pure assertion evaluator — testable without any subprocess
# ---------------------------------------------------------------------------

def evaluate_assertions(uart_log: str, gpio_states: dict[str, int],
                        assertions: list[str]) -> list[AssertionResult]:
    """Evaluate ``assertions`` against captured ``uart_log`` and ``gpio_states``.

    Each assertion is a string in one of two forms:
    - ``"uart:<text>"`` — passes when ``<text>`` appears anywhere in ``uart_log``.
    - ``"gpio:<PIN>=<level>"`` — passes when ``gpio_states[PIN] == int(level)``.
    - plain string (no prefix) — treated as ``"uart:<text>"`` for convenience.

    Returns a list of :class:`AssertionResult` in the same order as ``assertions``.
    This function is **pure** — no I/O, subprocess-safe to test without Renode.
    """
    results: list[AssertionResult] = []
    for assertion in assertions:
        a = assertion.strip()
        if a.startswith("gpio:"):
            # gpio:LED=1  or  gpio:PA5=0
            spec = a[len("gpio:"):]
            if "=" in spec:
                pin, _, lvl_str = spec.partition("=")
                pin = pin.strip()
                try:
                    expected = int(lvl_str.strip())
                    actual = gpio_states.get(pin)
                    if actual is None:
                        results.append(AssertionResult(
                            name=a, passed=False,
                            detail=f"pin '{pin}' not in gpio_states"))
                    elif actual == expected:
                        results.append(AssertionResult(name=a, passed=True,
                                                        detail=f"{pin}={actual}"))
                    else:
                        results.append(AssertionResult(
                            name=a, passed=False,
                            detail=f"{pin}={actual} != {expected}"))
                except ValueError:
                    results.append(AssertionResult(
                        name=a, passed=False, detail=f"invalid level '{lvl_str}'"))
            else:
                results.append(AssertionResult(
                    name=a, passed=False, detail="gpio assertion must be gpio:PIN=LEVEL"))
        else:
            # uart: prefix or plain text — UART substring match
            text = a[len("uart:"):] if a.startswith("uart:") else a
            if text in uart_log:
                results.append(AssertionResult(
                    name=a, passed=True, detail=f"found in uart_log"))
            else:
                results.append(AssertionResult(
                    name=a, passed=False, detail=f"'{text}' not found in uart_log"))
    return results


def _cap(text: str) -> str:
    return text if len(text) <= _UART_CAP else (text[:_UART_CAP] + "\n…[truncated]")


def _emulator_not_found(binary: str, t0: float) -> SimResult:
    """Return a graceful SimResult when the emulator binary is absent from PATH."""
    return SimResult(
        passed=False,
        uart_log=f"emulator_not_found: '{binary}' is not installed / not on PATH",
        ms=(time.perf_counter() - t0) * 1000.0,
    )


# ---------------------------------------------------------------------------
# Renode emulator
# ---------------------------------------------------------------------------

def _build_resc(elf_path: str, platform_file: str, run_seconds: float = 5.0) -> str:
    """Generate a minimal Renode ``.resc`` script that loads the ELF, starts emulation, and lets
    it run for ``run_seconds`` of wall-clock time before the script (and Renode with it) exits.

    The script:
    1. Creates a machine with the given platform description.
    2. Loads the ELF.
    3. Starts emulation.
    4. Sleeps ``run_seconds`` (the monitor's ``sleep`` command, real time — NOT the same as
       ``emulation`` virtual time) so the CPU actually executes past its startup code before the
       machine is disposed.

    T-C5 fix: step 4 was missing entirely — ``start`` returns to the monitor immediately (it
    launches emulation on a background thread and does not block), so with nothing after it the
    script reached EOF and Renode disposed the machine within ~1 emulated millisecond, before any
    meaningful firmware execution. Confirmed live: a firmware image that writes to UART in its
    main loop produced zero UART output without this — "Machine started" immediately followed by
    "Machine paused"/"Disposed". No unit test caught this because the mock suite validates
    assertion-evaluation logic (:func:`evaluate_assertions`) independently of a live Renode
    subprocess (T-C3 docstring's own admission — the mocks never actually exercised the .resc
    script's real effect).

    UART capture: ``--console`` alone does NOT forward a UART peripheral's bytes to stdout — a
    T-C5 fix, confirmed live (``start`` + ``sleep`` alone produced zero UART text even once the
    firmware was genuinely executing and transmitting). Renode requires an explicit
    ``showAnalyzer sysbus.<uart-name>`` monitor command to attach that peripheral's terminal
    output to the console sink (see Renode's own ``scripts/single-node/stm32f4_discovery.resc``).
    Only emitted when ``platform_file`` actually declares ``usart1`` — issuing it against a
    platform with no such peripheral (``cortex_m_generic.repl``) errors out per-command (Renode
    logs and continues) but there is no UART to capture there regardless, so it's skipped
    entirely for a cleaner log.

    Live-path note [~]: UART capture fidelity depends on the platform file defining a UART
    peripheral that Renode maps to its console sink.  ``cortex_m_generic.repl`` (no UART
    peripheral) will not produce UART output; ``stm32f4_discovery.repl`` (defines usart1)
    will.  This is expected — the mock suite validates assertion logic independently of
    live hardware.  Run ``renode --disable-xwt --console <script.resc>`` manually to
    verify UART capture for a specific platform.
    """
    has_uart = False
    try:
        has_uart = "usart1" in Path(platform_file).read_text(encoding="utf-8")
    except OSError:
        pass

    lines = [
        "mach create",
        f"machine LoadPlatformDescription @{platform_file}",
        f"sysbus LoadELF @{elf_path}",
    ]
    if has_uart:
        lines.append("showAnalyzer sysbus.usart1")
    lines.append("start")
    lines.append(f"sleep {run_seconds:.1f}")
    return "\n".join(lines) + "\n"


class RenodeEmulator:
    """Drives Renode (MIT, subprocess) to emulate an ELF against a platform description.

    Renode is invoked headless (``--disable-xwt``) with a generated ``.resc`` script.
    UART capture is done by reading Renode's output from process stdout (via the
    ``--console`` flag).  Renode forwards UART peripheral bytes to its console sink when
    run headless; platform files that define a UART peripheral (e.g. ``stm32f4_discovery``)
    will emit UART text on stdout.  The generic ``cortex_m_generic`` platform has no UART
    peripheral so live UART capture is unavailable there — see ``_build_resc`` for details.
    """

    BINARY = "renode"

    def run(self, elf_path: str, platform_file: str, assertions: list[str],
            timeout_s: float = _DEFAULT_TIMEOUT_S) -> SimResult:
        """Launch Renode, capture output, evaluate assertions.

        Args:
            elf_path: Absolute path to the ``.elf`` to emulate.
            platform_file: Path to the Renode ``.repl`` platform description.
            assertions: Assertion strings (see :func:`evaluate_assertions`).
            timeout_s: Wall-clock timeout for the emulation run.

        Returns:
            :class:`SimResult` — ``passed=False`` when Renode is absent (graceful).
        """
        t0 = time.perf_counter()

        if not shutil.which(self.BINARY):
            return _emulator_not_found(self.BINARY, t0)

        # Leave headroom under the subprocess timeout for Renode's own startup/teardown (~1s) and
        # the `renode --console` process exit, so `sleep <run_seconds>` finishes comfortably
        # before `subprocess.run`'s timeout_s would otherwise kill it mid-run.
        run_seconds = max(1.0, timeout_s - 5.0)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".resc", delete=False) as f:
            f.write(_build_resc(elf_path, platform_file, run_seconds=run_seconds))
            resc_path = f.name

        cmd = [self.BINARY, "--disable-xwt", "--console", resc_path]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False,
            )
            raw = proc.stdout or ""
            if proc.returncode not in (0, 1):
                # Non-zero but not normal exit — include stderr in log
                raw += proc.stderr or ""
        except FileNotFoundError:
            return _emulator_not_found(self.BINARY, t0)
        except subprocess.TimeoutExpired:
            return SimResult(
                passed=False,
                uart_log=f"timeout: renode exceeded {timeout_s:.0f}s",
                ms=(time.perf_counter() - t0) * 1000.0,
            )
        finally:
            try:
                Path(resc_path).unlink(missing_ok=True)
            except Exception:
                pass

        uart_log = _cap(raw)
        gpio_states: dict[str, int] = {}  # GPIO state extraction from Renode log (future)
        assertion_results = evaluate_assertions(uart_log, gpio_states, assertions)
        passed = all(a.passed for a in assertion_results) if assertion_results else True
        ms = (time.perf_counter() - t0) * 1000.0
        return SimResult(passed=passed, uart_log=uart_log, gpio_states=gpio_states,
                         assertion_results=assertion_results, ms=round(ms, 2))


# ---------------------------------------------------------------------------
# QEMU fallback emulator
# ---------------------------------------------------------------------------

class QEMUEmulator:
    """QEMU fallback for generic Cortex-M emulation (GPL-2.0+, subprocess-only).

    Uses ``qemu-system-arm -M mps2-an385`` with semihosting enabled; captures
    standard output (semihosting console) via the process stdout.
    """

    BINARY = "qemu-system-arm"

    def run(self, elf_path: str, timeout_s: float = _DEFAULT_TIMEOUT_S,
            assertions: Optional[list[str]] = None) -> SimResult:
        """Launch QEMU, capture semihosting output, evaluate assertions.

        Args:
            elf_path: Absolute path to the ``.elf`` to emulate.
            timeout_s: Wall-clock timeout.
            assertions: Assertion strings (see :func:`evaluate_assertions`).

        Returns:
            :class:`SimResult` — ``passed=False`` when QEMU is absent (graceful).
        """
        t0 = time.perf_counter()
        assertions = assertions or []

        if not shutil.which(self.BINARY):
            return _emulator_not_found(self.BINARY, t0)

        cmd = [
            self.BINARY,
            "-M", "mps2-an385",
            "-cpu", "cortex-m3",
            "-kernel", elf_path,
            "-semihosting",
            "-semihosting-config", "enable=on,target=native",
            "-nographic",
            "-no-reboot",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False,
            )
            raw = proc.stdout or ""
        except FileNotFoundError:
            return _emulator_not_found(self.BINARY, t0)
        except subprocess.TimeoutExpired:
            return SimResult(
                passed=False,
                uart_log=f"timeout: qemu-system-arm exceeded {timeout_s:.0f}s",
                ms=(time.perf_counter() - t0) * 1000.0,
            )

        uart_log = _cap(raw)
        gpio_states: dict[str, int] = {}
        assertion_results = evaluate_assertions(uart_log, gpio_states, assertions)
        passed = all(a.passed for a in assertion_results) if assertion_results else True
        ms = (time.perf_counter() - t0) * 1000.0
        return SimResult(passed=passed, uart_log=uart_log, gpio_states=gpio_states,
                         assertion_results=assertion_results, ms=round(ms, 2))


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------

def run_simulation(elf_path: str, mcu_family: str, assertions: list[str],
                   timeout_s: float = _DEFAULT_TIMEOUT_S) -> SimResult:
    """Top-level entry: pick Renode (with matching platform) or QEMU fallback.

    Neither present → graceful SimResult with explanation in ``uart_log``.
    """
    from engine.firmware.platform_db import get_platform_file  # local import to avoid cycles

    platform = get_platform_file(mcu_family)
    if shutil.which(RenodeEmulator.BINARY) and platform is not None:
        return RenodeEmulator().run(str(elf_path), str(platform), assertions, timeout_s)
    elif shutil.which(QEMUEmulator.BINARY):
        return QEMUEmulator().run(str(elf_path), timeout_s, assertions)
    else:
        # Neither emulator available — honest graceful degradation
        t0 = time.perf_counter()
        return SimResult(
            passed=False,
            uart_log=(
                "emulator_not_found: neither 'renode' nor 'qemu-system-arm' found on PATH. "
                "Install Renode (https://renode.io) or QEMU for live emulation. "
                "Tests that mock the subprocess run without either."
            ),
            ms=(time.perf_counter() - t0) * 1000.0,
        )
