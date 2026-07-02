"""Firmware compile-loop MCP tools — ``fw_new_project`` / ``fw_build`` / ``fw_sources`` (T-C2).

Thin FastMCP wrappers over :mod:`engine.firmware.mcp_tools`, mirroring ``services/mcp/ecad_tools.py``
exactly: each ``@mcp.tool()`` returns a JSON string and calls the framework-free engine handler
directly. Registration is deferred behind :func:`register_firmware_tools` so ``server.py`` wires us
in with a two-line block and the tools can be driven against a stub ``mcp`` in tests without the
whole stdio server.

The compile loop these expose: create a project whose MCU target and ``pinmap.h`` come from the
linked ECAD board → build with a real cross-toolchain → read parsed warnings/errors → fix → repeat.
"""

from __future__ import annotations

import json
from typing import Optional

from engine.firmware import mcp_tools as _fw


def _dumps(payload) -> str:
    return json.dumps(payload, indent=2)


def register_firmware_tools(mcp) -> None:
    """Register every firmware compile-loop tool on the given FastMCP instance."""

    @mcp.tool()
    def fw_new_project(name: str = "", mcu_family: str = "", toolchain: str = "cmake",
                       board_doc_ref: str = "") -> str:
        """Create an embedded-firmware project (CMake + arm-none-eabi baseline) and return its id.

        Pass ``board_doc_ref`` (an open ECAD board doc_id or a ``.ecad+``/``.json`` path) to link the
        firmware to a PCB: the MCU family is then auto-selected from the board's BOM when
        ``mcu_family`` is left empty, and a ``build/include/pinmap.h`` header of
        ``#define PIN_<NET> <n>`` is generated from the board's net names. Returns
        ``{project_id, project, pinmap_path?}``.
        """
        params: dict = {"toolchain": toolchain or "cmake"}
        if name:
            params["name"] = name
        if mcu_family:
            params["mcu_family"] = mcu_family
        if board_doc_ref:
            params["board_doc_ref"] = board_doc_ref
        return _dumps(_fw.fw_new_project(params))

    @mcp.tool()
    def fw_build(project_id: str) -> str:
        """Build a firmware project with its toolchain and return the BuildResult:
        ``{success, elf_path, map_path, stderr, warnings, returncode, cmd, ms}``. A missing
        cross-toolchain is reported as ``success=false, returncode=-1`` (not an error) so the agent
        can surface an install hint rather than crash the loop.
        """
        return _dumps(_fw.fw_build({"project_id": project_id}))

    @mcp.tool()
    def fw_sources(project_id: str, add: Optional[list[str]] = None,
                   remove: Optional[list[str]] = None) -> str:
        """List the project's source files, or add/remove them (paths relative to the project root).
        Adding a new path creates an empty stub so the next build sees it. Returns ``{src_paths}``.
        """
        params: dict = {"project_id": project_id}
        if add:
            params["add"] = add
        if remove:
            params["remove"] = remove
        return _dumps(_fw.fw_sources(params))

    @mcp.tool()
    def fw_sim_run(project_id: str, elf_path: str = "", scenario: str = "",
                   assertions: Optional[list[str]] = None) -> str:
        """Run the firmware emulation loop via Renode or QEMU and return SimResult.

        SimResult fields: ``{passed, uart_log, gpio_states, assertion_results, ms}``.
        ``passed`` is True iff every assertion matched. Assertions:
        - ``"uart:<text>"`` — UART/semihosting output contains ``<text>``.
        - ``"gpio:<PIN>=<level>"`` — GPIO pin is at the given logic level at end of run.
        - plain string — treated as uart match.

        Falls back gracefully when neither Renode nor QEMU is installed (``passed=False``,
        ``uart_log`` explains what's missing). Run ``fw_build`` first to produce the ELF.
        """
        p: dict = {"project_id": project_id}
        if elf_path:
            p["elf_path"] = elf_path
        if scenario:
            p["scenario"] = scenario
        if assertions:
            p["assertions"] = assertions
        return _dumps(_fw.fw_sim_run(p))

    # -----------------------------------------------------------------------
    # Hardware path (T-C4): probe-rs flash/debug + esptool ESP32 flash
    # -----------------------------------------------------------------------

    @mcp.tool()
    def fw_flash(elf_path: str, chip: str = "", port: str = "",
                 baud: int = 460800, flash_offset: str = "0x0") -> str:
        """Flash an ELF/bin to a target board and return FlashResult.

        Uses probe-rs (MIT/Apache-2.0) when ``port`` is absent or the image is an ``.elf``;
        falls back to esptool.py (GPL-2.0+, subprocess-only) for ESP32 when ``port`` is
        provided and the image is a ``.bin``.

        FlashResult fields: ``{success, output, returncode, cmd, ms}``.
        ``success=False`` with a descriptive ``output`` when the tool binary is absent —
        never an error envelope. Install hint: ``cargo install probe-rs-tools`` (probe-rs)
        or ``pip install esptool`` (ESP32).

        ``chip`` is the probe-rs chip identifier (e.g. ``"STM32F407VGTx"``,
        ``"nRF52840_xxAA"``). See ``probe-rs chip list`` for the full catalogue.

        [~] Live flash requires a connected DAP probe (probe-rs) or USB serial (esptool).
        """
        p: dict = {"elf_path": elf_path}
        if chip:
            p["chip"] = chip
        if port:
            p["port"] = port
        if baud != 460800:
            p["baud"] = baud
        if flash_offset != "0x0":
            p["flash_offset"] = flash_offset
        return _dumps(_fw.fw_flash(p))

    @mcp.tool()
    def fw_debug_start(chip: str) -> str:
        """Reset and start the target via probe-rs; returns FlashResult.

        Brings the board out of halt or error state so normal execution resumes.
        Uses ``probe-rs reset --chip <chip>``.

        [~] Live run requires a connected DAP probe.
        """
        return _dumps(_fw.fw_debug_start({"chip": chip}))

    @mcp.tool()
    def fw_debug_halt(chip: str) -> str:
        """Reset the target into halt via probe-rs; returns FlashResult.

        Stops execution so memory/registers can be inspected with fw_debug_read_mem.
        Uses ``probe-rs reset --chip <chip> --halt``.

        [~] Live run requires a connected DAP probe.
        """
        return _dumps(_fw.fw_debug_halt({"chip": chip}))

    @mcp.tool()
    def fw_debug_read_mem(chip: str, address: str, length: int) -> str:
        """Read *length* bytes from *address* on a halted target via probe-rs.

        Returns ``{success, data_hex, address, length, ms}``. ``data_hex`` is the raw
        bytes in lowercase hex. When probe-rs is absent or the read fails, ``success=False``
        with ``reason`` explaining the failure.

        ``address`` accepts decimal or hex strings (e.g. ``"0x20000000"``).
        Call fw_debug_halt first to halt the core.

        [~] Live run requires a connected DAP probe and a halted target.
        """
        return _dumps(_fw.fw_debug_read_mem({"chip": chip, "address": address, "length": length}))
