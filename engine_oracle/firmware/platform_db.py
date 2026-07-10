"""MCU family → Renode platform file lookup + capability matrix (T-C3, T-FIN2).

Platform files are bundled under ``engine_oracle/firmware/templates/renode_platforms/``
— curated from Renode's own upstream ``platforms/cpus/*.repl`` (MIT) wherever a stock
model exists; each file's header documents exactly what was adapted and why.

T-FIN2 adds the queryable capability matrix: per family, which peripherals the bundled
platform actually models (``uart`` / ``gpio`` / ``adc`` / ``i2c`` / ``spi`` / ``timers``)
plus how (or whether) it can be emulated at all. Callers — chiefly
``emulator.run_simulation`` — use this to refuse HONESTLY when an assertion needs a
peripheral the platform lacks, instead of silently returning a failed match.

Honesty ledger (2026-07-10 survey of upstream Renode master):
- stm32f4 / stm32f1: full UART + GPIO port + I2C + SPI + timer models; NO ADC model
  ships upstream, so ``adc=False`` everywhere.
- nrf52: upstream ``nrf52840.repl`` adopted near-verbatim (UART, GPIO, TWI/I2C, SPI,
  TIMER/RTC). GPIO *observation* (our AddStateChangedHook + ODR snapshot wiring) is
  STM32-register based, so ``gpio_observe=False`` — gpio assertions refuse honestly.
- rp2040: stock Renode has NO RP2040 SoC model; our minimal locally-authored platform
  gives one core + PL011 UARTs only (see rp2040.repl header).
- esp32: NO usable Renode emulation — build+flash only (see esp32_stub.repl header).

The lookup is case-insensitive on the mcu_family string. ``get_platform_file`` returns
``None`` if no specific platform is registered — callers fall back to the generic
Cortex-M platform or QEMU.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_PLATFORM_DIR = Path(__file__).parent / "templates" / "renode_platforms"

# mcu_family substring patterns → canonical family key (checked in order, first match wins)
_FAMILY_PATTERNS: list[tuple[str, str]] = [
    (r"stm32f4", "stm32f4"),
    (r"stm32f1", "stm32f1"),
    (r"nrf52",   "nrf52"),
    (r"rp2040|pico", "rp2040"),
    (r"esp32",   "esp32"),
    (r"cortex-m", "cortex-m"),
]

# Per-family capability matrix. Keys:
#   file        bundled .repl filename
#   emulation   "renode" | "renode-minimal" | "none"
#   uart        platform models a UART Renode can attach an analyzer to
#   gpio        platform declares GPIO port peripherals
#   gpio_observe  our hook+ODR observation wiring works (gpio:PIN=... assertions)
#   adc / i2c / spi / timers  corresponding Renode peripheral models present
#   gpio_regmap  {base, stride, odr_offset} for the end-of-run ODR snapshot (STM32 only)
#   notes       one-line honest summary surfaced in fw_status / cad_guide
PLATFORM_CAPS: dict[str, dict] = {
    "stm32f4": {
        "file": "stm32f4_discovery.repl",
        "emulation": "renode",
        "uart": True, "gpio": True, "gpio_observe": True,
        "adc": False, "i2c": True, "spi": True, "timers": True,
        "gpio_regmap": {"base": 0x4002_0000, "stride": 0x400, "odr_offset": 0x14},
        "notes": "USART1/2, GPIO A-E (observed), I2C1-3, SPI1-3, TIM1-5. No ADC model upstream.",
    },
    "stm32f1": {
        "file": "stm32f103.repl",
        "emulation": "renode",
        "uart": True, "gpio": True, "gpio_observe": True,
        "adc": False, "i2c": True, "spi": False, "timers": True,
        "gpio_regmap": {"base": 0x4001_0800, "stride": 0x400, "odr_offset": 0x0C},
        "notes": "Upstream stm32f103.repl: USART1-5, GPIO A-G (observed), I2C1-2, TIM1-14, DMA. No ADC/SPI model.",
    },
    "nrf52": {
        "file": "nrf52840.repl",
        "emulation": "renode",
        "uart": True, "gpio": True, "gpio_observe": False,
        "adc": False, "i2c": True, "spi": True, "timers": True,
        "gpio_regmap": None,
        "notes": "Upstream nrf52840.repl: UART0/1, GPIO0/1+GPIOTE, TWI0/1, SPI2, TIMER0-4/RTC. "
                 "gpio assertions unsupported (observation wiring is STM32-only).",
    },
    "rp2040": {
        "file": "rp2040.repl",
        "emulation": "renode-minimal",
        "uart": True, "gpio": False, "gpio_observe": False,
        "adc": False, "i2c": False, "spi": False, "timers": False,
        "gpio_regmap": None,
        "notes": "Minimal local platform (stock Renode has no RP2040 model): Cortex-M0+ + PL011 "
                 "UART0/1 only. Bare-metal UART works; pico-sdk stdio / GPIO / other peripherals do not.",
    },
    "esp32": {
        "file": "esp32_stub.repl",
        "emulation": "none",
        "uart": False, "gpio": False, "gpio_observe": False,
        "adc": False, "i2c": False, "spi": False, "timers": False,
        "gpio_regmap": None,
        "notes": "NO EMULATION — Renode ESP32 support is experimental with no usable peripherals. "
                 "Build (idf.py) + flash (esptool) only.",
    },
    "cortex-m": {
        "file": "cortex_m_generic.repl",
        "emulation": "renode",
        "uart": False, "gpio": False, "gpio_observe": False,
        "adc": False, "i2c": False, "spi": False, "timers": False,
        "gpio_regmap": None,
        "notes": "Generic fallback: CPU+NVIC+memory only; no peripherals. UART/gpio assertions "
                 "need a specific family platform (or the QEMU semihosting path).",
    },
}

# Kept for backwards compatibility with the T-C3 API (pattern → filename).
_PATTERNS: list[tuple[str, str]] = [
    (pat, PLATFORM_CAPS[fam]["file"]) for pat, fam in _FAMILY_PATTERNS
]


def canonical_family(mcu_family: str) -> str:
    """Map a free-form mcu_family string to a canonical PLATFORM_CAPS key.

    Unknown strings fall back to ``"cortex-m"`` (the generic platform) — same
    fallback posture as :func:`get_platform_file`.
    """
    key = (mcu_family or "").lower()
    for pattern, family in _FAMILY_PATTERNS:
        if re.search(pattern, key):
            return family
    return "cortex-m"


def get_capabilities(mcu_family: str) -> dict:
    """Return the capability entry for ``mcu_family`` (see PLATFORM_CAPS keys),
    with the canonical ``family`` key added. Never returns None."""
    family = canonical_family(mcu_family)
    caps = dict(PLATFORM_CAPS[family])
    caps["family"] = family
    return caps


def capability_matrix() -> dict[str, dict]:
    """Full queryable matrix: ``{family: {uart, gpio, adc, i2c, spi, timers, emulation, notes}}``.

    A stable, JSON-safe view for fw_status / cad_guide (drops gpio_regmap internals).
    """
    out: dict[str, dict] = {}
    for family, caps in PLATFORM_CAPS.items():
        out[family] = {
            k: caps[k]
            for k in ("file", "emulation", "uart", "gpio", "gpio_observe",
                      "adc", "i2c", "spi", "timers", "notes")
        }
    return out


def get_platform_file(mcu_family: str) -> Path | None:
    """Return the path to the bundled ``.repl`` for ``mcu_family``, or ``None`` if none matches."""
    family = canonical_family(mcu_family)
    candidate = _PLATFORM_DIR / PLATFORM_CAPS[family]["file"]
    if candidate.exists():
        return candidate
    # Generic Cortex-M fallback
    generic = _PLATFORM_DIR / "cortex_m_generic.repl"
    if generic.exists():
        return generic
    return None


def gpio_regmap_for(mcu_family: str) -> Optional[dict]:
    """GPIO ODR snapshot register map for the family, or None when unsupported."""
    return get_capabilities(mcu_family).get("gpio_regmap")


def list_platforms() -> dict[str, str]:
    """Return ``{mcu_pattern: filename}`` for all registered platforms."""
    return {pat: fn for pat, fn in _PATTERNS}
