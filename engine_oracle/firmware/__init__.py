"""AI-Firmware — embedded-firmware projects, cross-toolchain builds, and lightweight simulation.

A sibling of :mod:`engine.ecad` that adds the firmware pillar to AI-CAD. Python owns the project
record + persistence + toolchain wrappers; the C++ host and the MCP bridge call this engine
directly (see ``contracts/firmware_rpc.md``).

A firmware project may link to an ECAD board document via ``board_doc_ref`` — the one-way id/path
reference the agent uses to pull MCU/pin context from the board when generating sources.

Every toolchain backend (CMake+arm-none-eabi, ESP-IDF, Arduino CLI) is driven as an **unmodified
subprocess**, never linked — GPL-clean by aggregation, matching the ngspice posture in
``contracts/licenses.md``.
"""

from __future__ import annotations

__all__ = ["project", "toolchains"]
