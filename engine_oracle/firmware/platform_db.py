"""MCU family → Renode platform file lookup (T-C3).

Platform files are bundled under ``engine_oracle/firmware/templates/renode_platforms/``.
The lookup is case-insensitive on the mcu_family string. Returns ``None`` if no specific
platform is registered — callers fall back to the generic Cortex-M platform or QEMU.
"""
from __future__ import annotations

import re
from pathlib import Path

_PLATFORM_DIR = Path(__file__).parent / "templates" / "renode_platforms"

# mcu_family substring patterns → platform file name (checked in order, first match wins)
_PATTERNS: list[tuple[str, str]] = [
    (r"stm32f4", "stm32f4_discovery.repl"),
    (r"stm32f1", "stm32f103.repl"),
    (r"esp32",   "esp32_stub.repl"),
    (r"cortex-m", "cortex_m_generic.repl"),
]


def get_platform_file(mcu_family: str) -> Path | None:
    """Return the path to the bundled ``.repl`` for ``mcu_family``, or ``None`` if none matches."""
    key = mcu_family.lower()
    for pattern, filename in _PATTERNS:
        if re.search(pattern, key):
            candidate = _PLATFORM_DIR / filename
            if candidate.exists():
                return candidate
    # Generic Cortex-M fallback
    generic = _PLATFORM_DIR / "cortex_m_generic.repl"
    if generic.exists():
        return generic
    return None


def list_platforms() -> dict[str, str]:
    """Return ``{mcu_pattern: filename}`` for all registered platforms."""
    return {pat: fn for pat, fn in _PATTERNS}
