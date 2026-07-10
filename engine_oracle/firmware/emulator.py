"""Renode and QEMU emulator wrappers for the firmware emulation loop (T-C3).

Both :class:`RenodeEmulator` and :class:`QEMUEmulator` follow the same posture as
:mod:`engine.firmware.toolchains`: subprocess-only, never imported or linked; a missing
binary is graceful (``passed=False``, ``uart_log`` carries the reason), never a raise.

Renode is MIT-licensed (subprocess, never vendored). QEMU is GPL-2.0+ (subprocess only).
Platform files bundled in ``templates/renode_platforms/`` are also MIT; see
``contracts/licenses.md`` for the ledger row.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_DEFAULT_TIMEOUT_S = 30.0
_UART_CAP = 32_000  # keep captured UART log bounded

# Machine-readable GPIO event line, emitted by BOTH backends:
# - Renode: per-pin AddStateChangedHook print in the generated .resc (T-FIN1)
# - QEMU:   the firmware-side semihosting shim macro (templates/cmake_arm/include/aicad_trace.h)
# Format: ``AICAD_GPIO_EVT <PIN>=<level>[ t=<num>[ms|us|s]]`` — timestamp optional.
_GPIO_EVT_RE = re.compile(
    r"AICAD_GPIO_EVT\s+(?P<pin>[A-Za-z_][A-Za-z0-9_]*)=(?P<level>[01])"
    r"(?:\s+t=(?P<t>[0-9]+(?:\.[0-9]+)?)(?P<unit>ms|us|s)?)?")

# Renode final-state snapshot markers (echoed around a ``sysbus ReadDoubleWord <ODR>``)
_ODR_BEGIN_RE = re.compile(r"AICAD_GPIO_ODR_BEGIN\s+(?P<port>[A-K])")
_ODR_END_RE = re.compile(r"AICAD_GPIO_ODR_END\s+(?P<port>[A-K])")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")

# Default GPIO register map (STM32F4): GPIOA base 0x40020000, 0x400 stride, ODR at +0x14.
# T-FIN2: per-family maps now live in platform_db.PLATFORM_CAPS["gpio_regmap"] (e.g. the
# STM32F1 map differs: base 0x40010800, ODR at +0x0C); these constants stay as the default.
_STM32_GPIO_BASE = 0x4002_0000
_STM32_GPIO_STRIDE = 0x400
_STM32_ODR_OFFSET = 0x14

# First declared UART peripheral in a .repl, e.g. `usart1: UART.STM32_UART @ ...` or
# `uart0: UART.NRF52840_UART @ ...` — used to attach Renode's console analyzer (T-FIN2;
# previously hard-coded to "usart1", which missed nRF52/RP2040 UART names).
_REPL_UART_RE = re.compile(r"^(?P<name>\w+)\s*:\s*UART\.", re.MULTILINE)

# gpio assertion pin like PA5 / PB12 (STM32 convention) — used to derive Renode hooks
_STM32_PIN_RE = re.compile(r"^P(?P<port>[A-K])(?P<num>\d{1,2})$")


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
    ``gpio_events`` is the time-ordered pin-event timeline (T-FIN1) and ``trace_path``
    points at the structured JSON trace artifact (contract §7), when one was written.
    When an emulator binary is absent ``passed=False`` and ``uart_log`` explains why —
    this is the "graceful degradation" equivalent of :class:`BuildResult` (``success=False``).
    """
    passed: bool
    uart_log: str = ""
    gpio_states: dict[str, int] = field(default_factory=dict)
    assertion_results: list[AssertionResult] = field(default_factory=list)
    ms: float = 0.0
    gpio_events: list[dict] = field(default_factory=list)
    trace_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "uart_log": self.uart_log,
            "gpio_states": self.gpio_states,
            "assertion_results": [a.to_dict() for a in self.assertion_results],
            "ms": round(self.ms, 2),
            "gpio_events": self.gpio_events,
            "trace_path": self.trace_path,
        }


# ---------------------------------------------------------------------------
# GPIO trace parsing — pure functions, testable with canned emulator output
# ---------------------------------------------------------------------------

def parse_gpio_events(text: str) -> list[dict]:
    """Extract ``AICAD_GPIO_EVT`` pin events from raw emulator output.

    Returns a time-ordered (log-order) list of
    ``{"type": "gpio", "seq": int, "t_ms": float | None, "pin": str, "level": int}``.
    ``t_ms`` is None when the emitting side had no timestamp (Renode hook prints omit it;
    the QEMU semihosting shim includes it when the firmware supplies one). Pure — no I/O.
    """
    events: list[dict] = []
    for i, line in enumerate(text.splitlines()):
        m = _GPIO_EVT_RE.search(line)
        if not m:
            continue
        t_ms: Optional[float] = None
        if m.group("t") is not None:
            t = float(m.group("t"))
            unit = m.group("unit") or "ms"
            t_ms = t * 1000.0 if unit == "s" else (t / 1000.0 if unit == "us" else t)
        events.append({"type": "gpio", "seq": i, "t_ms": t_ms,
                       "pin": m.group("pin"), "level": int(m.group("level"))})
    return events


def parse_renode_gpio_odr(text: str) -> dict[str, int]:
    """Parse the ODR snapshot blocks the generated .resc echoes after the run.

    Blocks look like (``sysbus ReadDoubleWord`` prints the hex value between the markers)::

        AICAD_GPIO_ODR_BEGIN A
        0x00000020
        AICAD_GPIO_ODR_END A

    Returns end-of-run pin levels expanded per bit: ``{"PA0": 0, ..., "PA15": 0}``.
    Pure — no I/O.
    """
    states: dict[str, int] = {}
    port: Optional[str] = None
    value: Optional[int] = None
    for line in text.splitlines():
        b = _ODR_BEGIN_RE.search(line)
        if b:
            port, value = b.group("port"), None
            continue
        e = _ODR_END_RE.search(line)
        if e:
            if port == e.group("port") and value is not None:
                for bit in range(16):
                    states[f"P{port}{bit}"] = (value >> bit) & 1
            port, value = None, None
            continue
        if port is not None and value is None:
            h = _HEX_RE.search(line)
            if h:
                value = int(h.group(0), 16)
    return states


def derive_gpio_states(events: list[dict], odr_states: Optional[dict[str, int]] = None) -> dict[str, int]:
    """Final pin → level mapping: last event per pin, overridden by the ODR snapshot
    (the ODR read happens after the run sleep, so it is the freshest source)."""
    final: dict[str, int] = {}
    for ev in events:
        final[ev["pin"]] = ev["level"]
    final.update(odr_states or {})
    return final


def _pins_from_assertions(assertions: list[str]) -> list[tuple[str, int]]:
    """Extract STM32-style ``(port_letter, pin_number)`` pairs referenced by
    ``gpio:P<X><n>...`` assertions — these get Renode state-change hooks + ODR reads."""
    pins: list[tuple[str, int]] = []
    for a in assertions:
        a = a.strip()
        if not a.startswith("gpio:"):
            continue
        pin_name = a[len("gpio:"):].partition("=")[0].strip()
        m = _STM32_PIN_RE.match(pin_name)
        if m:
            pair = (m.group("port"), int(m.group("num")))
            if pair[1] <= 15 and pair not in pins:
                pins.append(pair)
    return pins


def write_trace_artifact(elf_path: str, source: str, uart_log: str,
                         gpio_events: list[dict], gpio_states: dict[str, int]) -> Optional[str]:
    """Write the structured JSON trace artifact next to the ELF (contract §7).

    Schema ``aicad.firmware.trace/1``: interleaved, seq-ordered pin + UART events plus the
    final GPIO snapshot. Atomic tmp+replace; any failure returns None (never raises) —
    the trace is an artifact, not a gate. Skipped (None) when the ELF itself does not
    exist — keeps mocked-subprocess tests from littering trace files next to fake paths.
    """
    try:
        if not Path(elf_path).exists():
            return None
        events: list[dict] = list(gpio_events)
        for i, line in enumerate(uart_log.splitlines()):
            s = line.strip()
            if not s or "AICAD_GPIO_EVT" in s or "AICAD_GPIO_ODR_" in s:
                continue
            events.append({"type": "uart", "seq": i, "t_ms": None, "text": s})
        events.sort(key=lambda e: e["seq"])
        payload = {
            "schema": "aicad.firmware.trace/1",
            "source": source,
            "generated_at": time.time(),
            "events": events,
            "gpio_final": gpio_states,
        }
        out = Path(elf_path).with_suffix(Path(elf_path).suffix + ".trace.json")
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(out)
        return str(out)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pure assertion evaluator — testable without any subprocess
# ---------------------------------------------------------------------------

_GPIO_TIME_RE = re.compile(r"^(?P<pin>[^=@]+)=(?P<level>\S+?)@(?P<op>[<>]?)(?P<t>[0-9]+(?:\.[0-9]+)?)ms$")


def evaluate_assertions(uart_log: str, gpio_states: dict[str, int],
                        assertions: list[str],
                        gpio_events: Optional[list[dict]] = None) -> list[AssertionResult]:
    """Evaluate ``assertions`` against captured ``uart_log`` and ``gpio_states``.

    Each assertion is a string in one of these forms:
    - ``"uart:<text>"`` — passes when ``<text>`` appears anywhere in ``uart_log``.
    - ``"gpio:<PIN>=<level>"`` — passes when ``gpio_states[PIN] == int(level)``.
    - ``"gpio:<PIN>=<level>@><T>ms"`` / ``@<<T>ms`` / ``@<T>ms`` — timeline form (T-FIN1):
      passes when a *time-stamped* event in ``gpio_events`` shows the pin at that level
      after / before / at-or-after ``T`` milliseconds. Fails with an explanatory detail
      when no time-stamped events exist for the pin.
    - plain string (no prefix) — treated as ``"uart:<text>"`` for convenience.

    Returns a list of :class:`AssertionResult` in the same order as ``assertions``.
    This function is **pure** — no I/O, subprocess-safe to test without Renode.
    """
    results: list[AssertionResult] = []
    gpio_events = gpio_events or []
    for assertion in assertions:
        a = assertion.strip()
        if a.startswith("gpio:"):
            # gpio:LED=1  or  gpio:PA5=0  or  gpio:PA5=1@>10ms
            spec = a[len("gpio:"):]
            tm = _GPIO_TIME_RE.match(spec.strip())
            if tm:
                pin = tm.group("pin").strip()
                op = tm.group("op")
                t_ref = float(tm.group("t"))
                try:
                    expected = int(tm.group("level"))
                except ValueError:
                    results.append(AssertionResult(
                        name=a, passed=False, detail=f"invalid level '{tm.group('level')}'"))
                    continue
                stamped = [e for e in gpio_events
                           if e.get("pin") == pin and e.get("t_ms") is not None]
                if not stamped:
                    results.append(AssertionResult(
                        name=a, passed=False,
                        detail=f"no time-stamped events for pin '{pin}'"))
                    continue
                if op == ">":
                    hits = [e for e in stamped if e["t_ms"] > t_ref and e["level"] == expected]
                elif op == "<":
                    hits = [e for e in stamped if e["t_ms"] < t_ref and e["level"] == expected]
                else:
                    hits = [e for e in stamped if e["t_ms"] >= t_ref and e["level"] == expected]
                if hits:
                    results.append(AssertionResult(
                        name=a, passed=True,
                        detail=f"{pin}={expected} at t={hits[0]['t_ms']:g}ms"))
                else:
                    seen = ", ".join(f"{e['level']}@{e['t_ms']:g}ms" for e in stamped[:8])
                    results.append(AssertionResult(
                        name=a, passed=False,
                        detail=f"no event {pin}={expected} {op or '>='}{t_ref:g}ms (events: {seen})"))
                continue
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

def _build_resc(elf_path: str, platform_file: str, run_seconds: float = 5.0,
                gpio_pins: Optional[list[tuple[str, int]]] = None,
                gpio_regmap: Optional[dict] = None) -> str:
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
    platform_text = ""
    try:
        platform_text = Path(platform_file).read_text(encoding="utf-8")
    except OSError:
        pass
    # T-FIN2: attach the console analyzer to the FIRST UART the platform declares
    # (usart1 on STM32, uart0 on nRF52/RP2040) instead of hard-coding "usart1".
    uart_m = _REPL_UART_RE.search(platform_text)
    uart_name = uart_m.group("name") if uart_m else None

    # GPIO monitoring (T-FIN1): only wire hooks/reads for ports the platform actually
    # declares — issuing monitor commands against a missing peripheral errors per-command.
    gpio_pins = [(p, n) for (p, n) in (gpio_pins or [])
                 if f"gpioPort{p}" in platform_text]

    regmap = gpio_regmap or {"base": _STM32_GPIO_BASE, "stride": _STM32_GPIO_STRIDE,
                             "odr_offset": _STM32_ODR_OFFSET}

    lines = [
        "mach create",
        f"machine LoadPlatformDescription @{platform_file}",
        f"sysbus LoadELF @{elf_path}",
    ]
    if uart_name:
        lines.append(f"showAnalyzer sysbus.{uart_name}")
    # Per-pin state-change hooks print a machine-readable event line the parser picks up.
    # [~] live-verify: hook print goes to the Renode console; timestamps are added by the
    # parser as None (Renode hook context has no stable virtual-time accessor across versions).
    for port, num in gpio_pins:
        lines.append(
            f"sysbus.gpioPort{port} AddStateChangedHook {num} "
            f"\"print('AICAD_GPIO_EVT P{port}{num}=%d' % (1 if state else 0))\"")
    lines.append("start")
    lines.append(f"sleep {run_seconds:.1f}")
    # Final-state snapshot: read each referenced port's ODR between echo markers so the
    # parser can pair the hex value with its port (STM32F4 register map).
    for port in sorted({p for (p, _) in gpio_pins}):
        odr = regmap["base"] + regmap["stride"] * (ord(port) - ord("A")) + regmap["odr_offset"]
        lines.append(f'echo "AICAD_GPIO_ODR_BEGIN {port}"')
        lines.append(f"sysbus ReadDoubleWord {odr:#x}")
        lines.append(f'echo "AICAD_GPIO_ODR_END {port}"')
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
            timeout_s: float = _DEFAULT_TIMEOUT_S,
            gpio_regmap: Optional[dict] = None) -> SimResult:
        """Launch Renode, capture output, evaluate assertions.

        Args:
            elf_path: Absolute path to the ``.elf`` to emulate.
            platform_file: Path to the Renode ``.repl`` platform description.
            assertions: Assertion strings (see :func:`evaluate_assertions`).
            timeout_s: Wall-clock timeout for the emulation run.
            gpio_regmap: Per-family GPIO ODR register map (T-FIN2,
                ``platform_db.gpio_regmap_for``); defaults to the STM32F4 map.

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
        gpio_pins = _pins_from_assertions(assertions)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".resc", delete=False) as f:
            f.write(_build_resc(elf_path, platform_file, run_seconds=run_seconds,
                                gpio_pins=gpio_pins, gpio_regmap=gpio_regmap))
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
        # GPIO extraction (T-FIN1): hook events + end-of-run ODR snapshot from the raw log.
        gpio_events = parse_gpio_events(raw)
        gpio_states = derive_gpio_states(gpio_events, parse_renode_gpio_odr(raw))
        trace_path = write_trace_artifact(elf_path, "renode", raw, gpio_events, gpio_states)
        assertion_results = evaluate_assertions(uart_log, gpio_states, assertions,
                                                gpio_events=gpio_events)
        passed = all(a.passed for a in assertion_results) if assertion_results else True
        ms = (time.perf_counter() - t0) * 1000.0
        return SimResult(passed=passed, uart_log=uart_log, gpio_states=gpio_states,
                         assertion_results=assertion_results, ms=round(ms, 2),
                         gpio_events=gpio_events, trace_path=trace_path)


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
        # GPIO extraction (T-FIN1): the firmware-side semihosting shim
        # (templates/cmake_arm/include/aicad_trace.h) prints AICAD_GPIO_EVT lines to the
        # semihosting console; QEMU has no external pin observer, so the shim is the source.
        gpio_events = parse_gpio_events(raw)
        gpio_states = derive_gpio_states(gpio_events)
        trace_path = write_trace_artifact(elf_path, "qemu", raw, gpio_events, gpio_states)
        assertion_results = evaluate_assertions(uart_log, gpio_states, assertions,
                                                gpio_events=gpio_events)
        passed = all(a.passed for a in assertion_results) if assertion_results else True
        ms = (time.perf_counter() - t0) * 1000.0
        return SimResult(passed=passed, uart_log=uart_log, gpio_states=gpio_states,
                         assertion_results=assertion_results, ms=round(ms, 2),
                         gpio_events=gpio_events, trace_path=trace_path)


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------

def _assertion_capability(assertion: str) -> str:
    """Which platform capability an assertion needs: ``"gpio"`` for gpio:*, else ``"uart"``."""
    return "gpio" if assertion.strip().startswith("gpio:") else "uart"


def _unsupported_result(caps: dict, assertions: list[str], unsupported: list[str],
                        reason: str, t0: float) -> SimResult:
    """Honest refusal: every assertion gets a result; unsupported ones explain why."""
    results = [
        AssertionResult(
            name=a, passed=False,
            detail=(f"unsupported on platform '{caps['file']}' "
                    f"(family {caps['family']}): {reason} — {caps['notes']}")
            if a in unsupported else "not evaluated: run refused (see unsupported assertions)")
        for a in assertions
    ]
    return SimResult(
        passed=False,
        uart_log=f"platform_capability: {reason} (family {caps['family']}, "
                 f"platform {caps['file']}). {caps['notes']}",
        assertion_results=results,
        ms=(time.perf_counter() - t0) * 1000.0,
    )


def run_simulation(elf_path: str, mcu_family: str, assertions: list[str],
                   timeout_s: float = _DEFAULT_TIMEOUT_S) -> SimResult:
    """Top-level entry: pick Renode (with matching platform) or QEMU fallback.

    T-FIN2 honesty gates (checked BEFORE launching anything):
    - a family with ``emulation == "none"`` (ESP32) refuses outright — build+flash only;
    - on the Renode path, assertions that need a peripheral the bundled platform does not
      model (``uart`` capability for uart:/plain assertions, ``gpio_observe`` for gpio:*)
      fall back to QEMU's semihosting path when installed, else refuse with a
      per-assertion explanation instead of silently failing to match.
    The QEMU fallback is exempt from the peripheral gate: both UART text and GPIO events
    arrive via the firmware-side semihosting shim, not platform peripheral models.

    Neither emulator present → graceful SimResult with explanation in ``uart_log``.
    """
    from engine.firmware.platform_db import get_capabilities, get_platform_file  # local import to avoid cycles

    t0 = time.perf_counter()
    caps = get_capabilities(mcu_family)

    if caps["emulation"] == "none":
        return _unsupported_result(
            caps, assertions, list(assertions),
            "no emulation path for this family (build + flash only)", t0)

    platform = get_platform_file(mcu_family)
    if shutil.which(RenodeEmulator.BINARY) and platform is not None:
        unsupported = [a for a in assertions
                       if (_assertion_capability(a) == "gpio" and not caps["gpio_observe"])
                       or (_assertion_capability(a) == "uart" and not caps["uart"])]
        if unsupported:
            # The Renode platform can't observe what the assertions need. The QEMU
            # semihosting path can (firmware-side aicad_trace.h shim reports both UART
            # text and pin events), so fall back to it when present; otherwise refuse
            # honestly instead of running a sim that can never match.
            if shutil.which(QEMUEmulator.BINARY):
                return QEMUEmulator().run(str(elf_path), timeout_s, assertions)
            missing = sorted({"gpio observation" if _assertion_capability(a) == "gpio"
                              else "a UART model" for a in unsupported})
            return _unsupported_result(
                caps, assertions, unsupported,
                f"platform lacks {' and '.join(missing)} required by the assertions "
                f"and no QEMU semihosting fallback is installed", t0)
        return RenodeEmulator().run(str(elf_path), str(platform), assertions, timeout_s,
                                    gpio_regmap=caps.get("gpio_regmap"))
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
