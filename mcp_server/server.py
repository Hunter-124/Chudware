"""AI-CAD MCP server.

Exposes the modeling engine as MCP tools so any MCP-capable agent (Claude Code, etc.)
can drive parametric CAD: write build123d code, render screenshots, read mass
properties, and export STEP/STL. Runs over stdio; the desktop host registers it into the
machine's Claude config so it "just works".

Single-part tools:
  run_model(code, name)   build a solid from build123d code; returns stats + paths
  get_screenshot(view)    render the current model (contact sheet or a single view)
  get_model_info()        mass properties + export paths for the current model
  export_model(fmt)       ensure STEP/STL exists; returns the path
  reset()                 clear the current model

Multi-part / assembly tools:
  run_assembly(code, name)      build several named parts placed together; returns per-part
                                stats + a fit report (interference / clearance)
  get_fit_report()              interference (overlap mm^3) + clearances (mm) for the assembly
  get_assembly_screenshot()     color-coded contact sheet of the assembled parts
  export_part(name, fmt)        STEP/STL path for one part (or the whole "assembly")
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

from engine import paths, render
from engine.kernel import AssemblyResult, ModelResult, run_code, run_code_multi

mcp = FastMCP(
    "AI-CAD",
    instructions=(
        "Parametric CAD via build123d. For a SINGLE part, call run_model(code) where `code` "
        "assigns the final shape to `result`, then get_screenshot() to verify it from "
        "iso/front/back/right/top/bottom. For a MULTI-PART assembly that must fit together, call "
        "run_assembly(code) where `code` defines `parts = {\"base\": solid, ...}` (each "
        "positioned with Pos/Rot); it returns per-part stats plus a FIT REPORT (pairwise "
        "interference overlap + clearances). Use get_assembly_screenshot() to see the "
        "color-coded fit and get_fit_report() for the numbers; verify no unintended "
        "interference before finishing. The OpenCASCADE B-rep kernel gives precise solids; "
        "prefer fillet/chamfer/holes/patterns over mesh hacks."
    ),
)

# in-memory "current model" — one active part per server session
_STATE: dict = {"name": None, "result": None, "out_dir": None}
# separate "current assembly" so the multi-part tools don't clobber the single-part state
_ASM: dict = {"name": None, "result": None, "out_dir": None}


def _summary(res: ModelResult) -> dict:
    return {
        "ok": res.ok,
        "error": res.error,
        "stdout": res.stdout,
        "triangles": res.triangles,
        "volume_mm3": res.volume,
        "area_mm2": res.area,
        "bbox": res.bbox,
        "step_path": res.step_path,
        "stl_path": res.stl_path,
        "warnings": res.warnings,
    }


@mcp.tool()
def run_model(code: str, name: str = "model") -> str:
    """Build a solid from build123d `code` (assign the final shape to `result`).

    Returns a JSON summary: ok/error, captured stdout, triangle count, volume (mm^3),
    surface area (mm^2), bounding box, and STEP/STL paths. On error, `error` holds the
    traceback — read it, fix the code, and call again.
    """
    out_dir = paths.model_dir(name)
    res = run_code(code, out_dir, name)
    _STATE.update(name=name, result=res, out_dir=str(out_dir))
    return json.dumps(_summary(res), indent=2)


@mcp.tool()
def get_screenshot(view: str = "all") -> Image:
    """Render the current model to a PNG and return it for visual inspection.

    view="all" -> a 2x3 contact sheet (iso/front/back/right/top/bottom). Otherwise one of
    "iso" | "front" | "back" | "right" | "top" | "bottom".
    """
    res: Optional[ModelResult] = _STATE["result"]
    if res is None or not res.ok or not res.stl_path:
        raise ValueError("No valid current model. Call run_model first.")
    out_dir = Path(_STATE["out_dir"])
    name = _STATE["name"]
    if view == "all":
        png = render.render_grid(res.stl_path, out_dir / f"{name}_grid.png")
    else:
        pngs = render.render_views(res.stl_path, out_dir, prefix=name, views=(view,))
        png = pngs[view]
    return Image(path=png)


@mcp.tool()
def get_model_info() -> str:
    """JSON mass properties + export paths for the current model."""
    res: Optional[ModelResult] = _STATE["result"]
    if res is None:
        return json.dumps({"error": "No current model. Call run_model first."})
    return json.dumps(_summary(res), indent=2)


@mcp.tool()
def export_model(fmt: str = "step") -> str:
    """Return the path to the current model in `fmt` ("step" or "stl"). Built at run time."""
    res: Optional[ModelResult] = _STATE["result"]
    if res is None or not res.ok:
        return json.dumps({"error": "No valid current model. Call run_model first."})
    path = res.step_path if fmt.lower() == "step" else res.stl_path
    if not path:
        return json.dumps({"error": f"No {fmt} export available."})
    return json.dumps({"format": fmt.lower(), "path": path})


@mcp.tool()
def reset() -> str:
    """Clear the current model and assembly."""
    _STATE.update(name=None, result=None, out_dir=None)
    _ASM.update(name=None, result=None, out_dir=None)
    return json.dumps({"ok": True})


# ----------------------------- multi-part / assembly -----------------------------

def _asm_summary(res: AssemblyResult) -> dict:
    return {
        "ok": res.ok,
        "error": res.error,
        "stdout": res.stdout,
        "is_assembly": res.is_assembly,
        "triangles": res.triangles,
        "volume_mm3": res.volume,
        "bbox": res.bbox,
        "assembly_step_path": res.step_path,
        "assembly_stl_path": res.stl_path,
        "parts": [
            {"name": p.get("name"), "volume_mm3": p.get("volume"), "bbox": p.get("bbox"),
             "triangles": p.get("triangles"), "step_path": p.get("step_path"),
             "stl_path": p.get("stl_path")}
            for p in res.parts
        ],
        "fit": res.fit,
        "warnings": res.warnings,
    }


@mcp.tool()
def run_assembly(code: str, name: str = "assembly") -> str:
    """Build a MULTI-PART assembly from build123d `code`.

    Define `parts = {"base": solid_a, "lid": solid_b, ...}` where each value is a complete
    solid positioned in the shared coordinate space with `Pos(x,y,z) *` / `Rot(rx,ry,rz) *`
    (and/or an explicit `assembly = Compound(children=[...])`). Returns a JSON summary: each
    part's volume/bbox/STEP+STL paths, a combined assembly STEP/STL, and a FIT REPORT —
    pairwise interference (overlap mm^3 when bodies collide) and clearances (gap mm when they
    don't). Then call get_assembly_screenshot() to see the color-coded fit and verify it
    before finishing. A plain single-`result` script also works (one part, no fit report).
    """
    out_dir = paths.model_dir(name)
    res = run_code_multi(code, out_dir, name)
    _ASM.update(name=name, result=res, out_dir=str(out_dir))
    return json.dumps(_asm_summary(res), indent=2)


@mcp.tool()
def get_fit_report() -> str:
    """JSON fit report for the current assembly: interference (overlapping pairs + overlap
    volume mm^3), clearances (non-touching pairs + gap mm), and per-part bounding boxes.
    Call run_assembly first."""
    res: Optional[AssemblyResult] = _ASM["result"]
    if res is None or not res.ok:
        return json.dumps({"error": "No valid current assembly. Call run_assembly first."})
    if not res.is_assembly or res.fit is None:
        return json.dumps({"is_assembly": False,
                           "note": "The current build is a single part; there is no fit report."})
    return json.dumps(res.fit, indent=2)


@mcp.tool()
def get_assembly_screenshot() -> Image:
    """Render the current assembly to a color-coded 2x2 contact sheet (each part a distinct
    color, with a legend) and return it for visual fit inspection. Call run_assembly first."""
    res: Optional[AssemblyResult] = _ASM["result"]
    if res is None or not res.ok or not res.stl_path:
        raise ValueError("No valid current assembly. Call run_assembly first.")
    out_dir = Path(_ASM["out_dir"])
    name = _ASM["name"]
    png = None
    if res.is_assembly and res.parts:
        png = render.render_assembly_grid(res.parts, out_dir / f"{name}_asm_grid.png")
    if not png:
        png = render.render_grid(res.stl_path, out_dir / f"{name}_grid.png")
    if not png:
        raise ValueError("Rendering unavailable (pyvista/vtk not installed).")
    return Image(path=png)


@mcp.tool()
def export_part(name: str, fmt: str = "step") -> str:
    """Path to one part of the current assembly in `fmt` ("step" or "stl"). `name` matches a
    part name from run_assembly's summary; pass "assembly" for the combined model. Built at
    run time. Call run_assembly first."""
    res: Optional[AssemblyResult] = _ASM["result"]
    if res is None or not res.ok:
        return json.dumps({"error": "No valid current assembly. Call run_assembly first."})
    fmt = fmt.lower()
    if name.lower() in ("assembly", "all", ""):
        path = res.step_path if fmt == "step" else res.stl_path
        return json.dumps({"name": "assembly", "format": fmt, "path": path} if path
                          else {"error": f"No {fmt} export for the assembly."})
    for p in res.parts:
        if p.get("name") == name:
            path = p.get("step_path") if fmt == "step" else p.get("stl_path")
            return json.dumps({"name": name, "format": fmt, "path": path} if path
                              else {"error": f"No {fmt} export for part {name!r}."})
    avail = [p.get("name") for p in res.parts]
    return json.dumps({"error": f"No part named {name!r}. Available: {avail}"})


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
