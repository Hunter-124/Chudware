"""CAM (computer-aided machining) MCP tools — toolpath generation, feeds & speeds,
material-removal simulation, and post-processed G-code, driven through the C++ core
(libaicad ``cam_*`` RPCs). Mirrors the in-app CAM workspace.

Call ``cad_guide("cam")`` for the workflow. The typical loop:
    cam_new_job -> cam_add_tool -> cam_extract (stock/holes/regions from the part)
    -> cam_add_setup -> cam_add_operation -> cam_generate -> cam_simulate -> cam_post.

Registered onto the FastMCP instance by ``register_cam_tools(mcp)``.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import core_bridge as cb
from .core_bridge import CoreError
from .cad_tools import _CUR  # the "current part" doc_id (shared with the modeling tools)

# The "current CAM job" so single-argument calls just work.
_CAM: dict = {"doc_id": None}


def _wire_materials_registry() -> None:
    """Point the C++ core at the bundled engine materials registry so the CuttingDB folds its
    ``cam`` sub-sections in (T-M4 / Issue #2). ``setdefault`` never overrides an explicit
    AICAD_MATERIALS_REGISTRY (a user/installer path wins). Best-effort: a missing registry module
    leaves the core on its in-code fallback table."""
    try:
        from engine.materials import registry as _reg
        os.environ.setdefault("AICAD_MATERIALS_REGISTRY", str(_reg.registry_path()))
    except Exception:  # noqa: BLE001
        pass


def _resolve_cam_material(material: str) -> tuple[str, Optional[str]]:
    """Map a material id/alias to the CuttingDB group id the feeds calculator resolves.

    Accepts any registry material ID (DoD): a registry id/alias that carries a ``cam`` fold-in is
    translated to its cutting-group id; a bare CuttingDB group id (or an unknown id) passes through
    unchanged. Returns (resolved_id, note) — ``note`` is a non-fatal advisory when the material has
    no cam correspondence (feeds will use the calculator's defaults)."""
    m = str(material or "").strip() or "aluminum"
    try:
        from engine.materials import registry as _reg
        gid = _reg.cam_material_id(m)
        if gid:
            return gid, None
        # Not a cam-annotated registry material. If it is a known registry material at all, keep the
        # user's id but flag that it has no dedicated feeds row; else pass through (may be a raw
        # CuttingDB group id the core knows).
        if _reg.resolve_key(m) is not None:
            return m, f"material '{m}' has no CAM feeds data in the registry; using calculator defaults"
    except Exception:  # noqa: BLE001
        pass
    return m, None


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2)


def _err(e: Exception) -> str:
    if isinstance(e, CoreError):
        return _dumps({"ok": False, "error": str(e), "detail": getattr(e, "detail", None)})
    return _dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _job() -> str:
    d = _CAM["doc_id"]
    if not d:
        raise ValueError("No CAM job. Call cam_new_job(...) first.")
    return d


def _apply(ops: list) -> dict:
    return cb.call("cam_apply_ops", {"doc_id": _job(), "base_revision": -1, "ops": ops})


def register_cam_tools(mcp: FastMCP) -> None:
    """Attach the CAM toolpath/post/sim tools to the FastMCP server."""

    # Point the core's CuttingDB at the bundled materials registry (T-M4) before any cam RPC
    # triggers the lazy CuttingDB::builtin() singleton, so cam_materials reflects the registry.
    _wire_materials_registry()

    # ------------------------------------------------------------------ job lifecycle
    @mcp.tool()
    def cam_new_job(name: str = "CAM Job", post: str = "grbl") -> str:
        """Create a new CAM machining job and make it current. `post` is the default
        controller dialect (grbl|linuxcnc|fanuc|haas|mach3|marlin|siemens|centroid;
        see cam_post_dialects). Returns the job tree + doc_id."""
        try:
            r = cb.call("cam_new", {"name": name, "post": post})
            _CAM["doc_id"] = r.get("doc_id")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_job_tree() -> str:
        """The current CAM job tree: setups, their stock/wcs/machine, operations, and the
        tool library, plus counts. Read this to see the job state."""
        try:
            return _dumps(cb.call("cam_tree", {"doc_id": _job()}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_undo() -> str:
        """Undo the last CAM edit."""
        try:
            return _dumps(cb.call("cam_undo", {"doc_id": _job()}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_redo() -> str:
        """Redo the last undone CAM edit."""
        try:
            return _dumps(cb.call("cam_redo", {"doc_id": _job()}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ tools / stock / setup
    @mcp.tool()
    def cam_add_tool(number: int, type: str = "endmill_flat", diameter: float = 6.0,
                     flutes: int = 2, corner_radius: float = 0.0, name: str = "",
                     tool_material: str = "carbide", tip_angle: float = 0.0,
                     thread_pitch: float = 0.0) -> str:
        """Add (or replace by number) a tool in the library. type: endmill_flat|endmill_ball|
        endmill_bull|chamfer_mill|face_mill|vbit|drill|spot_drill|center_drill|tap|reamer|
        boring_bar|thread_mill|turning_general|grooving_tool|threading_tool|parting_tool|
        laser_head|plasma_torch|waterjet_head|probe (cam_tool_types lists all)."""
        try:
            tool = {"number": number, "type": type, "diameter": diameter, "flutes": flutes,
                    "corner_radius": corner_radius, "name": name or f"T{number} {type}",
                    "tool_material": tool_material, "tip_angle": tip_angle, "thread_pitch": thread_pitch}
            return _dumps(_apply([{"op": "add_tool", "tool": tool}]))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_add_setup(name: str = "Setup1", machine: str = "mill3", material: str = "aluminum",
                      stock: Optional[dict] = None, wcs: Optional[dict] = None) -> str:
        """Add a machining setup (a stock + WCS + machine/platform). machine: mill3|mill4|mill5|
        router|lathe|millturn|laser|plasma|waterjet. `material` accepts any engine materials
        registry ID/alias (e.g. "aluminum_6061_t6", "6061") or a CuttingDB group id ("aluminum",
        "steel_mild") — a registry id is translated to the group whose feeds/speeds the calculator
        uses (T-M4). `stock` is a stock dict (e.g. from cam_extract(kind="stock")["stock"]["box"]
        -> {"type":"box","min":[...],"max":[...]}), defaulting to a box-offset of the model bbox.
        Returns the new setup_id (+ a material_note when the id has no dedicated feeds row)."""
        try:
            resolved, note = _resolve_cam_material(material)
            st = dict(stock or {"type": "box_offset", "off_ztop": 1.0})
            st["material"] = resolved
            setup = {"name": name, "machine": {"kind": machine}, "stock": st}
            if wcs:
                setup["wcs"] = wcs
            r = _apply([{"op": "add_setup", "setup": setup, "ref": "_s"}])
            r["setup_id"] = r.get("id_map", {}).get("_s")
            if note:
                r["material_note"] = note
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_set_stock(setup_id: str, stock: dict) -> str:
        """Set/replace a setup's stock. stock types: box | box_offset | cylinder |
        cylinder_offset | from_solid (see cad_guide('cam'))."""
        try:
            return _dumps(_apply([{"op": "set_stock", "setup_id": setup_id, "stock": stock}]))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ operations
    @mcp.tool()
    def cam_add_operation(type: str, tool_number: int, setup_id: Optional[str] = None,
                          name: str = "", geometry: Optional[dict] = None,
                          heights: Optional[dict] = None, params: Optional[dict] = None) -> str:
        """Add a machining operation to a setup. type: face|contour2d|pocket2d|slot|chamfer2d|
        bore2d|engrave|adaptive2d|drill|thread_mill|parallel3d|contour3d|scallop3d|pencil3d|
        pocket3d|adaptive_clear3d|radial3d|spiral_finish3d|turn_face|turn_rough|turn_finish|
        turn_groove|turn_thread|turn_part|turn_drill|cut2d|probe (cam_op_types lists all).
        `geometry`: {regions|contours|points|profile|mesh} (use cam_extract to get it).
        `heights`: {clearance,retract,top,bottom}. `params`: any extra op fields (stepover,
        stepdown, stock_to_leave, direction, lead_in/out, cycle, peck, comp_side, ...).
        Feeds/speeds auto-resolve from material+tool unless you pin rpm/feed/etc in params."""
        try:
            op: dict = {"type": type, "tool_number": tool_number}
            if name:
                op["name"] = name
            if geometry:
                op["geometry"] = geometry
            if heights:
                op["heights"] = heights
            if params:
                op.update(params)
            o = {"op": "add_operation", "operation": op, "ref": "_o"}
            if setup_id:
                o["setup_id"] = setup_id
            r = _apply([o])
            r["op_id"] = r.get("id_map", {}).get("_o")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_update_operation(op_id: str, params: dict) -> str:
        """Update fields on an existing operation (merge). e.g. {"stepover":2,"feed":900}."""
        try:
            return _dumps(_apply([{"op": "update_operation", "op_id": op_id, "operation": params}]))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_remove_operation(op_id: str) -> str:
        """Remove an operation by id."""
        try:
            return _dumps(_apply([{"op": "remove_operation", "op_id": op_id}]))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ extraction
    @mcp.tool()
    def cam_extract(kind: str = "all", z: Optional[float] = None,
                    z_levels: Optional[list] = None, path: Optional[str] = None,
                    deflection: float = 0.1) -> str:
        """Extract CAM geometry FROM THE CURRENT PART (or a `path` to a STEP/IGES/BREP/STL).
        kind: all | stock | mesh | section (at z) | sections (at z_levels) | holes | profile.
        Returns stock bounds, a mesh, planar Z-section regions, Z-axis drill points, and/or a
        turning profile — feed these straight into cam_add_setup/cam_add_operation geometry."""
        try:
            p = path
            if not p:
                # export the current part to a temp STEP, then extract
                doc = _CUR.get("doc_id")
                if not doc:
                    raise ValueError("No current part and no path=. Build/open a part first, or pass path=.")
                p = os.path.join(tempfile.gettempdir(), f"aicad_cam_extract_{os.getpid()}.step")
                cb.call("export_file", {"doc_id": doc, "path": p, "format": "step"})
            params: dict = {"path": p, "kind": kind, "deflection": deflection}
            if z is not None:
                params["z"] = z
            if z_levels is not None:
                params["z_levels"] = z_levels
            return _dumps(cb.call("cam_extract", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ generate / sim / post
    @mcp.tool()
    def cam_generate(setup_id: Optional[str] = None, op_id: Optional[str] = None) -> str:
        """Generate toolpaths for the job (or one setup / one op). Returns the toolpaths +
        any warnings (e.g. a tool too large for a pocket)."""
        try:
            params: dict = {"doc_id": _job()}
            if setup_id:
                params["setup_id"] = setup_id
            if op_id:
                params["op_id"] = op_id
            return _dumps(cb.call("cam_generate", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_simulate(setup_id: Optional[str] = None, resolution: float = 0.5,
                     check_gouge: bool = True, check_rapid: bool = True) -> str:
        """Material-removal simulation: removed/remaining volume, machined fraction, gouge and
        rapid-through-stock checks, and estimated cycle time per setup."""
        try:
            params: dict = {"doc_id": _job(),
                            "options": {"resolution": resolution, "check_gouge": check_gouge,
                                        "check_rapid": check_rapid}}
            if setup_id:
                params["setup_id"] = setup_id
            return _dumps(cb.call("cam_simulate", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_post(dialect: Optional[str] = None, setup_id: Optional[str] = None,
                 program_name: str = "AICAD", program_number: int = 1000,
                 sequence_numbers: bool = False) -> str:
        """Post-process to controller G-code. `dialect` overrides the job default. Returns the
        NC program text + line count, tool changes, and estimated time."""
        try:
            params: dict = {"doc_id": _job(),
                            "options": {"program_name": program_name,
                                        "program_number": program_number,
                                        "sequence_numbers": sequence_numbers}}
            if dialect:
                params["dialect"] = dialect
            if setup_id:
                params["setup_id"] = setup_id
            return _dumps(cb.call("cam_post", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_setup_sheet(dialect: Optional[str] = None) -> str:
        """Generate the operator setup sheet (HTML + text): tool list + per-operation cut
        length and time + total machining time."""
        try:
            params: dict = {"doc_id": _job()}
            if dialect:
                params["dialect"] = dialect
            return _dumps(cb.call("cam_setup_sheet", params))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ persistence
    @mcp.tool()
    def cam_save(path: str) -> str:
        """Save the CAM job to a .cam+ file."""
        try:
            return _dumps(cb.call("cam_save", {"doc_id": _job(), "path": path}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_open(path: str) -> str:
        """Open a .cam+ file and make it the current job."""
        try:
            r = cb.call("cam_open", {"path": path})
            _CAM["doc_id"] = r.get("doc_id")
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------------------ reference data
    @mcp.tool()
    def cam_post_dialects() -> str:
        """List the available post-processor controller dialects."""
        try:
            return _dumps(cb.call("cam_post_dialects", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_materials() -> str:
        """List the cutting-data material database (the feeds & speeds priors)."""
        try:
            return _dumps(cb.call("cam_materials", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_tool_types() -> str:
        """List the supported tool types and their cutting-data class."""
        try:
            return _dumps(cb.call("cam_tool_types", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_op_types() -> str:
        """List the supported operation types."""
        try:
            return _dumps(cb.call("cam_op_types", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)
