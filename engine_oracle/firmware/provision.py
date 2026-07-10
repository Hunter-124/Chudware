"""Firmware toolchain/emulator provisioning doctor — ``fw_status`` probe (T-FIN4).

Mirrors the RE-06 provisioning pattern (``engine_oracle/vision/reveng_provision.py``):
a descriptor table of external CLI tools plus a pure, offline-safe ``fw_status()``
readiness probe. Every firmware backend drives an unmodified external CLI as a
subprocess (see ``toolchains.py`` / ``emulator.py`` / ``hardware.py``); nothing here
is bundled or linked, and nothing here ever touches the network.

Gate-friendly contract: ``fw_status()`` NEVER raises and never "fails" — a machine
with zero firmware tools installed still gets ``ok=True`` with every tool reported
``found=False`` plus an actionable ``install_hint``, and an all-False capability
rollup per MCU family. Version probing shells out (``<tool> --version``) with a
short timeout; any subprocess hiccup degrades to ``version=None``, never an error.

Shape (frozen for callers — services/mcp/firmware_tools.py ``fw_status`` tool)::

    {ok: True,
     tools: {<name>: {found, path, version, install_hint, unlocks}},
     families: {<family>: {can_build, can_emulate, can_flash, missing: [str]}},
     banners: [str], missing: [str]}
"""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Optional

_VERSION_TIMEOUT_S = 5.0

# ---------------------------------------------------------------------------
# Tool descriptor table
# ---------------------------------------------------------------------------
# aliases: alternate executable names tried in order via shutil.which.
# version_args: argv appended to the resolved binary to elicit a version string.
# unlocks: human-readable capability tags surfaced per tool.
FW_TOOL_DESCRIPTORS: dict[str, dict] = {
    "cmake": {
        "aliases": ("cmake",),
        "version_args": ("--version",),
        "unlocks": ["build:cortex-m"],
        "install_hint": "Install CMake: `sudo dnf install cmake` (or `sudo apt install cmake`).",
    },
    "ninja": {
        "aliases": ("ninja",),
        "version_args": ("--version",),
        "unlocks": ["build:cortex-m (faster generator; optional)"],
        "install_hint": "Install Ninja: `sudo dnf install ninja-build` (or `sudo apt install ninja-build`).",
    },
    "arm-none-eabi-gcc": {
        "aliases": ("arm-none-eabi-gcc",),
        "version_args": ("--version",),
        "unlocks": ["build:cortex-m"],
        "install_hint": (
            "Install the ARM cross-toolchain: `sudo dnf install arm-none-eabi-gcc-cs "
            "arm-none-eabi-newlib` (or `sudo apt install gcc-arm-none-eabi`)."
        ),
    },
    "renode": {
        "aliases": ("renode",),
        "version_args": ("--version",),
        "unlocks": ["emulate:cortex-m", "emulate:esp32"],
        "install_hint": (
            "Install Renode from https://renode.io/#downloads (portable tarball or "
            "`sudo dnf copr enable ...`); put `renode` on PATH."
        ),
    },
    "qemu-system-arm": {
        "aliases": ("qemu-system-arm",),
        "version_args": ("--version",),
        "unlocks": ["emulate:cortex-m (fallback when Renode absent)"],
        "install_hint": "Install QEMU ARM: `sudo dnf install qemu-system-arm` (or `sudo apt install qemu-system-arm`).",
    },
    "probe-rs": {
        "aliases": ("probe-rs",),
        "version_args": ("--version",),
        "unlocks": ["flash:cortex-m", "debug:cortex-m"],
        "install_hint": "Install probe-rs: `cargo install probe-rs-tools` (MIT/Apache-2.0).",
    },
    "esptool": {
        "aliases": ("esptool.py", "esptool"),
        "version_args": ("version",),
        "unlocks": ["flash:esp32"],
        "install_hint": "Install esptool: `pip install esptool` (GPL-2.0+, subprocess-only).",
    },
    # Extra family toolchains (not required by the core 7, but honest to report):
    "idf.py": {
        "aliases": ("idf.py",),
        "version_args": ("--version",),
        "unlocks": ["build:esp32"],
        "install_hint": (
            "Install ESP-IDF (Apache-2.0): https://docs.espressif.com/projects/esp-idf/ "
            "then `. ./export.sh` so idf.py is on PATH."
        ),
    },
    "arduino-cli": {
        "aliases": ("arduino-cli",),
        "version_args": ("version",),
        "unlocks": ["build:avr"],
        "install_hint": "Install arduino-cli: https://arduino.github.io/arduino-cli/ (GPL-3, subprocess-only).",
    },
}

# MCU family capability rules. Each entry maps capability -> requirement spec:
#   "all_of": every listed tool must be found
#   "any_of": at least one listed tool must be found
# Families follow engine_oracle/firmware/mcu_pinouts.py vocabulary
# (cortex-m* -> "cortex-m", xtensa-lx6/esp32 -> "esp32", atmega/attiny -> "avr").
FW_FAMILY_RULES: dict[str, dict[str, dict[str, tuple[str, ...]]]] = {
    "cortex-m": {
        "can_build": {"all_of": ("cmake", "arm-none-eabi-gcc")},
        "can_emulate": {"any_of": ("renode", "qemu-system-arm")},
        "can_flash": {"all_of": ("probe-rs",)},
    },
    "esp32": {
        "can_build": {"all_of": ("idf.py",)},
        "can_emulate": {"any_of": ("renode",)},  # esp32_stub.repl; QEMU ARM can't run xtensa
        "can_flash": {"all_of": ("esptool",)},
    },
    "avr": {
        "can_build": {"all_of": ("arduino-cli",)},
        "can_emulate": {"any_of": ()},  # no bundled AVR emulation path yet
        "can_flash": {"any_of": ()},    # avrdude path not wired yet (arduino-cli upload needs a board)
    },
}

# The 7 core tools the T-FIN4 spec names — drive `missing` + banners; the extras
# (idf.py / arduino-cli) are reported in `tools` but only bannered via families.
_CORE_TOOLS = (
    "cmake", "ninja", "arm-none-eabi-gcc", "renode", "qemu-system-arm", "probe-rs", "esptool",
)


# --------------------------------------------------------------------------- probing


def _which(name: str) -> Optional[str]:
    """Resolve a tool descriptor to an absolute path via its aliases. Never raises."""
    d = FW_TOOL_DESCRIPTORS.get(name)
    if not d:
        return None
    for alias in d.get("aliases", (name,)):
        try:
            path = shutil.which(alias)
        except Exception:  # noqa: BLE001 — probe must never raise
            path = None
        if path:
            return path
    return None


_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+~][\w.]+)?")


def _probe_version(path: str, version_args: tuple[str, ...]) -> Optional[str]:
    """Best-effort `<tool> --version` -> short version string. Never raises."""
    try:
        proc = subprocess.run(
            [path, *version_args], capture_output=True, text=True,
            timeout=_VERSION_TIMEOUT_S, check=False,
        )
        text = (proc.stdout or "") + (proc.stderr or "")
    except Exception:  # noqa: BLE001 — FileNotFoundError, timeout, perms, anything
        return None
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not first:
        return None
    m = _VERSION_RE.search(first)
    return m.group(0) if m else first[:80]


def tool_status() -> dict[str, dict]:
    """Per-tool {found, path, version, install_hint, unlocks} for every descriptor."""
    out: dict[str, dict] = {}
    for name, d in FW_TOOL_DESCRIPTORS.items():
        path = _which(name)
        version = _probe_version(path, tuple(d.get("version_args", ("--version",)))) if path else None
        out[name] = {
            "found": path is not None,
            "path": path,
            "version": version,
            "install_hint": d["install_hint"],
            "unlocks": list(d.get("unlocks", [])),
        }
    return out


def _rollup(tools: dict[str, dict]) -> dict[str, dict]:
    def found(n: str) -> bool:
        return bool(tools.get(n, {}).get("found"))

    families: dict[str, dict] = {}
    for family, rules in FW_FAMILY_RULES.items():
        entry: dict = {}
        missing: list[str] = []
        for cap, spec in rules.items():
            all_of = spec.get("all_of", ())
            any_of = spec.get("any_of", ())
            ok = all(found(t) for t in all_of) if all_of else False
            if any_of:
                ok = ok or any(found(t) for t in any_of)
            if not (all_of or any_of):
                ok = False  # capability has no provisioned path yet
            entry[cap] = ok
            if not ok:
                missing.extend(t for t in (*all_of, *any_of) if not found(t))
        entry["missing"] = sorted(set(missing))
        families[family] = entry
    return families


def fw_status() -> dict:
    """Firmware provisioning doctor (T-FIN4). Pure, offline-safe, NEVER raises/fails.

    Returns ``{ok, tools, families, banners, missing}`` — see module docstring.
    """
    tools = tool_status()
    families = _rollup(tools)
    missing = [n for n in _CORE_TOOLS if not tools[n]["found"]]

    banners: list[str] = []
    fam = families["cortex-m"]
    if not fam["can_build"]:
        banners.append(
            "Cortex-M builds unavailable: install cmake + arm-none-eabi-gcc "
            "(see tools[*].install_hint)."
        )
    if not fam["can_emulate"]:
        banners.append("Firmware emulation unavailable: install Renode (preferred) or qemu-system-arm.")
    if not fam["can_flash"]:
        banners.append("Cortex-M flashing unavailable: `cargo install probe-rs-tools`.")
    if not families["esp32"]["can_flash"]:
        banners.append("ESP32 flashing unavailable: `pip install esptool`.")

    return {
        "ok": True,
        "tools": tools,
        "families": families,
        "banners": banners,
        "missing": missing,
    }
