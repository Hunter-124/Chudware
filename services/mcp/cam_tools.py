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
from .cad_common import _wire_materials_registry  # engine-side registry env default
from .cad_tools import _CUR  # the "current part" doc_id (shared with the modeling tools)

# The "current CAM job" so single-argument calls just work.
_CAM: dict = {"doc_id": None}

# Point the C++ core at the bundled materials registry at IMPORT time (T-M4 / Issue #2):
# the CuttingDB folds the registry's ``cam`` sub-sections in, and material_cam_id /
# material_resolve_key (used by _resolve_cam_material) read it. Import time matters — the
# core caches the registry once per process, so the env must be set before the first
# engine RPC can spawn/attach an engine without it.
_wire_materials_registry()


def _resolve_cam_material(material: str) -> tuple[str, Optional[str]]:
    """Map a material id/alias to the CuttingDB group id the feeds calculator resolves.

    Accepts any registry material ID (DoD): a registry id/alias that carries a ``cam`` fold-in is
    translated to its cutting-group id; a bare CuttingDB group id (or an unknown id) passes through
    unchanged. Returns (resolved_id, note) — ``note`` is a non-fatal advisory when the material has
    no cam correspondence (feeds will use the calculator's defaults). Resolution goes through the
    C++ core's ``material_cam_id`` / ``material_resolve_key`` methods."""
    m = str(material or "").strip() or "aluminum"
    try:
        gid = cb.call("material_cam_id", {"name": m}).get("result")
        if gid:
            return gid, None
        # Not a cam-annotated registry material. If it is a known registry material at all, keep the
        # user's id but flag that it has no dedicated feeds row; else pass through (may be a raw
        # CuttingDB group id the core knows).
        if cb.call("material_resolve_key", {"name": m}).get("result") is not None:
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


def add_tool_from_dict(tool: dict) -> dict:
    """Add (or replace by number) one tool in the current CAM job's library.

    Module-level seam shared by ``cam_add_tool`` and the parts-library
    ``cam_tool_import`` tool (services/mcp/parts_tools.py). Fills the same
    defaults cam_add_tool historically applied; raises when no CAM job is
    current (callers surface that as data)."""
    t = dict(tool or {})
    number = int(t.get("number", 0))
    t.setdefault("type", "endmill_flat")
    t.setdefault("diameter", 6.0)
    t.setdefault("flutes", 2)
    t.setdefault("corner_radius", 0.0)
    t.setdefault("tool_material", "carbide")
    t.setdefault("tip_angle", 0.0)
    t.setdefault("thread_pitch", 0.0)
    if not t.get("name"):
        t["name"] = f"T{number} {t['type']}"
    t["number"] = number
    return _apply([{"op": "add_tool", "tool": t}])


def existing_tool_numbers() -> list[int]:
    """Tool numbers already present in the current CAM job's library (sorted).

    Reads the same job tree ``cam_job_tree`` renders. Raises when no CAM job
    is current."""
    r = cb.call("cam_tree", {"doc_id": _job()})
    tree = r.get("tree") if isinstance(r.get("tree"), dict) else r
    nums: list[int] = []
    for t in (tree.get("tools") or []):
        try:
            nums.append(int(t.get("number")))
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(set(nums))


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
            return _dumps(add_tool_from_dict(tool))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def cam_add_setup(name: str = "Setup1", machine: str = "mill3", material: str = "aluminum",
                      stock: Optional[dict] = None, wcs: Optional[dict] = None,
                      machine_profile: str = "") -> str:
        """Add a machining setup (a stock + WCS + machine/platform). machine: mill3|mill4|mill5|
        router|lathe|millturn|laser|plasma|waterjet. `material` accepts any engine materials
        registry ID/alias (e.g. "aluminum_6061_t6", "6061") or a CuttingDB group id ("aluminum",
        "steel_mild") — a registry id is translated to the group whose feeds/speeds the calculator
        uses (T-M4). `stock` is a stock dict (e.g. from cam_extract(kind="stock")["stock"]["box"]
        -> {"type":"box","min":[...],"max":[...]}), defaulting to a box-offset of the model bbox.
        `machine_profile` (optional): id from cam_machines() (e.g. "haas_vf2") — merges the full
        machine envelope (travel, rapids, rpm/feed clamps, tool_change_s, accel) into the setup
        machine payload. Returns the new setup_id (+ a material_note when the id has no dedicated
        feeds row)."""
        try:
            resolved, note = _resolve_cam_material(material)
            st = dict(stock or {"type": "box_offset", "off_ztop": 1.0})
            st["material"] = resolved
            mach: dict = {"kind": machine}
            mp = str(machine_profile or "").strip()
            if mp:
                try:
                    mr = cb.call("cam_machines", {})
                except Exception as e:  # noqa: BLE001
                    raise ValueError(
                        f"machine profile '{mp}' unavailable — cam_machines failed: {e}"
                    ) from e
                machines = mr.get("machines") if isinstance(mr, dict) else None
                if not isinstance(machines, list):
                    raise ValueError(
                        f"machine profile '{mp}' unavailable — cam_machines returned no list"
                    )
                hit = None
                for m in machines:
                    if isinstance(m, dict) and str(m.get("id", "")) == mp:
                        hit = m
                        break
                if hit is None:
                    ids = sorted(str(m.get("id", "")) for m in machines if isinstance(m, dict))
                    raise ValueError(
                        f"unknown machine profile '{mp}'; valid ids: {ids}"
                    )
                # Map aicad.machines/1 → core Machine wire keys; drop catalog-only fields.
                if hit.get("kind"):
                    mach["kind"] = hit["kind"]
                if hit.get("label"):
                    mach["name"] = hit["label"]
                if "travel_mm" in hit:
                    mach["travel"] = hit["travel_mm"]
                elif "travel" in hit:
                    mach["travel"] = hit["travel"]
                if "rapid_xy_mm_min" in hit:
                    mach["rapid_xy"] = hit["rapid_xy_mm_min"]
                elif "rapid_xy" in hit:
                    mach["rapid_xy"] = hit["rapid_xy"]
                if "rapid_z_mm_min" in hit:
                    mach["rapid_z"] = hit["rapid_z_mm_min"]
                elif "rapid_z" in hit:
                    mach["rapid_z"] = hit["rapid_z"]
                for src, dst in (
                    ("max_rpm", "max_rpm"),
                    ("min_rpm", "min_rpm"),
                    ("max_feed_mm_min", "max_feed"),
                    ("max_feed", "max_feed"),
                    ("tool_change_s", "tool_change_s"),
                    ("accel_mm_s2", "accel"),
                    ("accel", "accel"),
                ):
                    if src in hit and dst not in mach:
                        mach[dst] = hit[src]
            setup = {"name": name, "machine": mach, "stock": st}
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
        cylinder_offset | from_solid | from_sim (see cad_guide('cam')).
        from_sim form: {"type":"from_sim","setup_id":"<id>"} uses the remaining
        stock mesh from a prior cam_simulate of that setup."""
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
                     check_gouge: bool = True, check_rapid: bool = True,
                     check_holder: bool = False) -> str:
        """Material-removal simulation: removed/remaining volume, machined fraction, gouge,
        rapid-through-stock, and holder/shank collision checks, and estimated cycle time per
        setup. check_holder is off by default (needs tool shank/holder geometry to flag)."""
        try:
            params: dict = {"doc_id": _job(),
                            "options": {"resolution": resolution, "check_gouge": check_gouge,
                                        "check_rapid": check_rapid, "check_holder": check_holder}}
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

    # ------------------------------------------------------------------ T-W6 feeds/speeds tools

    @mcp.tool()
    def feeds_speeds(
        material: str,
        tool_type: str,
        tool_dia: float,
        operation: str,
        max_rpm: Optional[float] = None,
        min_rpm: Optional[float] = None,
        max_feed: Optional[float] = None,
        min_feed: Optional[float] = None,
        thread: str = "",
        machine: str = "",
    ) -> str:
        """Compute table-driven rpm / feed / stepdown / stepover envelope for a
        material + tool type + operation (aicad.calc/1, contracts/advisor.md §7).

        Returns a recommended midpoint and the full [min, max] envelope so the
        advisory engine can range-check caller-supplied params. Use
        ``cam_check_feeds_envelope`` to get an advisory item when params are outside
        this range.

        Parameters
        ----------
        material : str
            Material id or alias — any id from ``cam_materials()`` or the simulation
            materials list (``alu_6061``, ``steel_1018``, ``ti_6al4v``, ``abs``,
            ``pla``, ``nylon_pa12``, ``cast_iron_gray``, ``inconel_718``, …).
        tool_type : str
            One of: ``endmill_flat``, ``endmill_ball``, ``drill``, ``tap``,
            ``face_mill``.
        tool_dia : float
            Tool diameter in mm.
        operation : str
            One of: ``roughing``, ``finishing``, ``slotting``, ``drilling``.
        max_rpm, min_rpm, max_feed, min_feed : float, optional
            Machine envelope overrides (mm/min for feeds, RPM for speed). Unset fields
            use conservative benchtop-VMC defaults (max_rpm=15000, max_feed=5000), or
            the selected ``machine`` profile when given.
        thread : str, optional
            When non-empty, include ``answer.tap_drill_mm`` from the tap-drill table
            (e.g. ``M10x1.5`` → 8.5). Prefer this over a separate tap_drill call.
        machine : str, optional
            Machine profile id (``haas_vf2``, ``tormach_770``, …) — clamps rpm/feed
            to that machine's envelope from machines.json / cam_machines().
        """
        from services.research import machining as _m  # noqa: PLC0415
        lim: dict = {}
        if max_rpm  is not None: lim["max_rpm"]  = max_rpm
        if min_rpm  is not None: lim["min_rpm"]  = min_rpm
        if max_feed is not None: lim["max_feed"] = max_feed
        if min_feed is not None: lim["min_feed"] = min_feed
        mat, note = _resolve_cam_material(material)
        result = _m.feeds_speeds(
            mat, tool_type, tool_dia, operation, lim or None,
            thread=thread or "", machine=machine or "",
        )
        if note:
            result.setdefault("notes", []).append(note)
        return _dumps(result)

    @mcp.tool()
    def cam_check_feeds_envelope(
        material: str,
        tool_type: str,
        tool_dia: float,
        operation: str,
        rpm: Optional[float] = None,
        feed: Optional[float] = None,
        stepdown: Optional[float] = None,
        stepover: Optional[float] = None,
        max_rpm: Optional[float] = None,
        min_rpm: Optional[float] = None,
        max_feed: Optional[float] = None,
        min_feed: Optional[float] = None,
    ) -> str:
        """Check whether supplied cutting params fall within the feeds/speeds table
        envelope. Returns an ``aicad.advisory/1`` item (contracts/advisor.md §3) with
        ``severity=hint`` and clamped ``candidate_fixes`` when any param is out of range,
        or ``{ok: true, in_envelope: true}`` when everything is within bounds.

        This is the CAM advisor seam: the CAM stage check battery calls this after
        ``cam_add_operation``; out-of-envelope params are surfaced as advisory hints
        before simulation discovers gouges.
        """
        from services.research import machining as _m  # noqa: PLC0415
        lim: dict = {}
        if max_rpm  is not None: lim["max_rpm"]  = max_rpm
        if min_rpm  is not None: lim["min_rpm"]  = min_rpm
        if max_feed is not None: lim["max_feed"] = max_feed
        if min_feed is not None: lim["min_feed"] = min_feed
        mat, _note = _resolve_cam_material(material)
        adv = _m.check_envelope(
            mat, tool_type, tool_dia, operation, lim or None,
            rpm=rpm, feed=feed, stepdown=stepdown, stepover=stepover,
        )
        if adv is None:
            return _dumps({"ok": True, "in_envelope": True,
                           "message": "All supplied params are within the feeds/speeds table envelope."})
        return _dumps(adv)

    @mcp.tool()
    def tap_drill(thread: str) -> str:
        """Return the standard tap drill diameter for a thread designation
        (aicad.calc/1, contracts/advisor.md §7).

        Accepts metric coarse (``M6x1.0``, ``M8``), metric fine, UNC
        (``1/4-20``, ``#10-24``) and UNF designations. Returns
        ``answer.tap_drill_mm`` (mm) and ``answer.pct_thread`` (typically 75%).
        Use ``research_thread_spec`` for full thread geometry (minor/pitch diameter,
        tensile stress area, etc.).
        """
        from services.research import machining as _m  # noqa: PLC0415
        return _dumps(_m.tap_drill(thread))

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
