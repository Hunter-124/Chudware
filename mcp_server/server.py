"""AI-CAD MCP server.

Exposes the AI-CAD modeling engine — the SAME C++ core (`libaicad.dll`) the desktop app
runs — as MCP tools, so an MCP client (Claude in cowork/chat or Claude Code) can drive
real parametric CAD and simulation: build an editable feature tree with the plain-English
geometry vocabulary, render an annotated image to self-review, run FEA (static/modal) and
wind-tunnel CFD, make 2D drawings, pull standard hardware, and export STEP/STL/3MF/etc.

This is parity with the in-app AI agent — except YOU (the model calling these tools) are the
brain, so call `cad_guide()` first to load the engine's op vocabulary, then model → render →
iterate → simulate exactly like the app does.

Mechanical CAD + simulation tools live in `cad_tools` (driven through `core_bridge`, the
ctypes bridge to the staged DLL). ECAD (schematic/PCB) tools live in `ecad_tools`.
Runs over stdio; the desktop host registers it into the machine's MCP client configs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "AI-CAD",
    instructions=(
        "Real parametric CAD + simulation on the AI-CAD C++ engine (same kernel as the "
        "desktop app). You are the design brain.\n"
        "START by calling cad_guide() — it returns the engine's modeling op vocabulary and "
        "worked examples; cad_guide('simulation') and cad_guide('review') cover FEA/CFD and "
        "how to read the annotated render.\n"
        "TYPICAL LOOP: new_part(name) -> add_features([...ops...]) to build with standard "
        "editable features (boxes/sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell, "
        "holes, bolt_circle, patterns/mirror) -> render_part() to SEE the colour-coded, "
        "face-labelled result and self-review against the request -> iterate add_features to "
        "fix anything -> measure_part()/query_geometry() to check -> run_fea_static()/"
        "run_fea_modal()/run_cfd_flow() to prove it -> export_part() or make_drawing() to "
        "deliver.\n"
        "Address geometry in PLAIN WORDS — 'the top face', 'all edges', 'the vertical edges', "
        "'the largest round face' — never face numbers or raw coordinates. Build with the "
        "feature a person would use (sketch+revolve for round bodies, loft for blended shapes), "
        "not stacks of boxes. The OpenCASCADE B-rep kernel gives precise solids.\n"
        "ECAD (schematic/PCB) tools are also available (ecad_*)."
    ),
)

# Mechanical CAD + simulation (C++ feature engine via libaicad.dll).
from mcp_server.cad_tools import register_cad_tools
register_cad_tools(mcp)

# ECAD (schematic / PCB / native circuit sim).
from mcp_server.ecad_tools import register_ecad_tools
register_ecad_tools(mcp)


def _install_clean_stdout():
    """Point the process's native stdout (fd 1) at a log file and return a private handle to
    the REAL stdout pipe for the MCP transport.

    Why: the C++ core (OCCT) prints statistics straight to fd 1 — which is the MCP JSON-RPC
    channel. Those bytes corrupt the protocol, and on a slow-drained pipe a big native write
    can even BLOCK and hang the call ("request timed out"). Redirecting fd 1 to a file once,
    here at startup, makes that impossible without any per-call fd toggling (which would race
    the transport's off-thread response writer). The transport is handed an explicit stream
    bound to the preserved pipe. Returns an ``anyio`` async file, or None if the dup dance
    isn't supported (then we fall back to the default transport).
    """
    try:
        import anyio
        from io import FileIO, TextIOWrapper

        real_fd = os.dup(1)  # the genuine JSON-RPC pipe to the MCP client
        log_path = os.path.join(tempfile.gettempdir(), "ai-cad-mcp-native.log")
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(log_fd, 1)  # fd 1 -> log; sys.stdout follows, so stray prints are harmless too
        os.close(log_fd)
        real_stream = anyio.wrap_file(TextIOWrapper(FileIO(real_fd, mode="w"), encoding="utf-8"))
        return real_stream
    except Exception:
        return None


def _warm_core() -> None:
    """Load libaicad.dll up front (in the background) so the FIRST tool call doesn't eat the
    cold load (DLL + OCCT/gmsh deps can take many seconds) inside the client's call timeout."""
    try:
        from mcp_server import core_bridge
        core_bridge.available()
    except Exception:
        pass


def main() -> None:
    real_stream = _install_clean_stdout()
    # Warm the core only AFTER fd 1 is redirected, so its load-time prints land in the log,
    # never on the protocol pipe.
    threading.Thread(target=_warm_core, name="aicad-warm", daemon=True).start()

    if real_stream is None:
        mcp.run()  # fallback (fd redirect unsupported) — stdio transport on sys.stdout
        return

    import anyio
    from mcp.server.stdio import stdio_server

    async def _serve() -> None:
        async with stdio_server(stdout=real_stream) as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream, write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    anyio.run(_serve)


if __name__ == "__main__":
    main()
