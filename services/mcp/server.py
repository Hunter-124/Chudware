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
        "NO VISION? Use describe_part()/describe_sketch()/describe_scene() (cad_guide('gsl')) "
        "for a text-only read of the geometry instead of render_part's image. Before calling a "
        "design done, run check_feature_overlap() (one part's own features colliding — a boss "
        "swallowed by a later cut, redundant cuts) and, for multi-body/assembly docs, "
        "check_interference() (clash/clearance between bodies or components).\n"
        "ECAD (schematic/PCB) tools are also available (ecad_*)."
    ),
)

# Mechanical CAD + simulation (C++ feature engine via libaicad.dll).
from mcp_server.cad_tools import register_cad_tools
register_cad_tools(mcp)

# GSL — Geometry-Semantics Layer: compact named part outline (describe_part), face-adjacency
# graph (query_adjacency), and stable-id hover. The progressive-pull view of the geometry.
from mcp_server.gsl_tools import register_gsl_tools
register_gsl_tools(mcp)

# Recon — reverse-engineering: import a STEP/mesh → deterministic feature recognition → an
# EDITABLE parametric tree, verified against the source solid by boolean comparison
# (reverse_engineer / compare_parts / convert_face_to_sketch; guide: cad_guide("reverse")).
from mcp_server.recon_tools import register_recon_tools
register_recon_tools(mcp)

# Intake — photo-measurement + manufacturing-clarification + first-try-fit. resolve/set process,
# request/submit photos, confirm critical dims, dfm_check, fit_report, make_fit_coupon.
from mcp_server.intake_tools import register_intake_tools
register_intake_tools(mcp)

# ECAD (schematic / PCB / native circuit sim).
from mcp_server.ecad_tools import register_ecad_tools
register_ecad_tools(mcp)

# CAM (computer-aided machining): toolpaths, feeds & speeds, material-removal sim, G-code post.
from mcp_server.cam_tools import register_cam_tools
register_cam_tools(mcp)

# Requirements (aicad.req/1): CRUD + markdown projection for design briefs.
# Schema: contracts/requirements.md  Brain: engine_oracle/requirements/requirements.py
from mcp_server.req_tools import register_req_tools
register_req_tools(mcp)


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
    # Arm ECAD autosave + recover any ECAD docs from a previous (possibly killed) session FIRST.
    # ECAD is pure Python with no DLL dependency, so this never blocks on the C++ load and an ECAD
    # recovery succeeds even if libaicad.dll is unavailable. ECAD docs live only in memory, so a
    # transport-timeout kill of a heavy ecad_* tool would otherwise lose every open schematic/board.
    try:
        from mcp_server.ecad_tools import enable_ecad_autosave, recover_ecad_autosaved
        enable_ecad_autosave()
        recover_ecad_autosaved()
    except Exception:
        pass
    # Pre-warm the heavy PURE-PYTHON import stacks the ECAD tools lazy-import on first use, so the
    # FIRST ecad_simulate / ecad_to_3d call doesn't pay a cold import INSIDE the client's transport
    # window. This matters more than usual here: a cold-import overrun trips -32001, which KILLS the
    # server, so the import never gets cached — every retry re-pays it and times out again (a vicious
    # loop). Importing them once in the background breaks that. Best-effort; never blocks the DLL warm.
    try:
        # scipy-backed SPICE simulator (ecad_simulate) — ~0.4 s cold.
        from engine.sim.spice import netlist_to_spice_circuit  # noqa: F401
        from engine.sim.circuit import simulate as _sim_warm  # noqa: F401
    except Exception:
        pass
    try:
        # OCP/build123d 3D kernel (ecad_to_3d) — can be many seconds cold.
        from engine.ecad.threed import board_to_step  # noqa: F401
    except Exception:
        pass
    try:
        from mcp_server import core_bridge
        core_bridge.available()
        # Recover any docs auto-saved before a previous restart so a client-timeout kill doesn't
        # permanently drop the user's open parts (they reload with fresh ids; list_parts shows them).
        try:
            from mcp_server.cad_tools import recover_autosaved
            recover_autosaved()
        except Exception:
            pass
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
