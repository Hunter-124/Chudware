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
