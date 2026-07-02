"""AI-Hydraulic MCP tools — ISO 1219 hydraulic circuit design + component sizing.

Same design contract as ``piping_tools`` (project owner: "the AI is extremely limited; its tools
must do the bookkeeping and return rich, actionable context"). Each tool returns a JSON string
``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}``, calls the engine helpers
directly (``engine.hydraulic.schema`` / ``engine.hydraulic.solve`` / ``engine.hydraulic.bom``),
and holds its own tiny session state so tools can reuse the last active circuit document.

Tools:
  hydraulic_new             create a new aicad.hydraulic/1 circuit document
  hydraulic_add_component   add an ISO 1219 component to the active circuit
  hydraulic_add_hose        add a hose/line net connecting two or more component ports
  hydraulic_size_cylinder   size a hydraulic cylinder (force, flow, cycle time)
  hydraulic_size_pump       size a hydraulic pump (displacement, power, torque)
  hydraulic_bom             extract a sourcing BOM from the active circuit

Registration is deferred behind :func:`register_hydraulic_tools` so ``server.py`` wires us in
with a tiny fenced block — mirrors :func:`register_piping_tools`.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Union

from engine.hydraulic import schema as hyd_schema
from engine.hydraulic import solve as hyd_solve
from engine.hydraulic import bom as hyd_bom

# Session bookkeeping: the last active circuit document
_HYD: dict = {"doc": None}


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def _load_doc(doc: Union[str, dict, None]) -> Any:
    """Accept a hydraulic doc as a dict, a JSON string, or a path to a ``.json`` file."""
    if doc is None:
        return None
    if isinstance(doc, dict):
        return doc
    if isinstance(doc, str):
        s = doc.strip()
        if s and s[0] != "{" and os.path.isfile(s):
            with open(s) as fh:
                return json.load(fh)
        return json.loads(s)
    raise TypeError(f"doc must be a dict, JSON string, or file path (got {type(doc).__name__})")


def _as_obj(x: Union[str, list, dict, None]) -> Any:
    """Parse an argument that may arrive as a JSON string or native object."""
    if x is None or isinstance(x, (list, dict)):
        return x
    if isinstance(x, str):
        s = x.strip()
        return json.loads(s) if s else None
    return x


def _active_doc() -> Optional[dict]:
    return _HYD.get("doc")


def register_hydraulic_tools(mcp) -> None:
    """Register the AI-Hydraulic tools on the given FastMCP instance.

    Called from ``mcp_server/server.py`` after ``mcp`` is created. A function (not import-time
    side effects) so the tools can be driven against a stub ``mcp`` in tests — mirrors
    :func:`register_piping_tools`.
    """

    @mcp.tool()
    def hydraulic_new(name: str = "Hydraulic Circuit") -> str:
        """Create a new ``aicad.hydraulic/1`` hydraulic circuit document.

        Initialises an empty circuit with ISO 1219 unit defaults (bar / L/min / kN / mm).
        The new document becomes the active session document for subsequent
        ``hydraulic_add_component``, ``hydraulic_add_hose``, and ``hydraulic_bom`` calls.

        Returns the initial document envelope with schema, id, and empty component/hose lists.
        """
        doc = hyd_schema.new_hydraulic(name)
        _HYD["doc"] = doc
        return _dumps({
            "ok": True,
            "doc": doc,
            "warnings": [],
            "hint": "Use hydraulic_add_component to add ISO 1219 components.",
            "next_actions": [
                "call hydraulic_add_component to add pumps, cylinders, valves, etc.",
                "call hydraulic_size_cylinder / hydraulic_size_pump to size components",
            ],
        })

    @mcp.tool()
    def hydraulic_add_component(
        type: str,
        name: str = "",
        x: float = 0.0,
        y: float = 0.0,
        params: Union[str, dict, None] = None,
        sourcing: Union[str, dict, None] = None,
        doc: Union[str, dict, None] = None,
    ) -> str:
        """Add an ISO 1219 hydraulic component to the active circuit document.

        ``type`` must be one of the ISO 1219 component types defined in
        ``contracts/hydraulic_schema.md``:
        pump, cylinder_linear, cylinder_rotary, valve_directional, valve_check,
        valve_relief, valve_flow_control, accumulator, reservoir, motor_hydraulic,
        filter, heat_exchanger.

        ``x``, ``y`` set the schematic canvas position (mm). ``params`` is a JSON object
        of type-specific parameters (bore_mm, stroke_mm, displacement_cc_rev, etc.) —
        see the contract for per-type param keys. ``sourcing`` carries mpn/manufacturer/
        description/quantity/unit/category for BOM roll-up.

        ``doc`` overrides the session document (optional — if omitted, uses the last
        ``hydraulic_new`` document).

        Returns the updated document with the new component appended.
        """
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            p = _as_obj(params) or {}
            s = _as_obj(sourcing) or {}
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        if d is None:
            return _dumps(_err("no active hydraulic circuit",
                               hint="call hydraulic_new first, or pass doc=<circuit>"))

        if type not in hyd_schema.HYD_COMPONENT_TYPES:
            return _dumps(_err(
                f"unknown component type {type!r}",
                hint=f"valid types: {sorted(hyd_schema.HYD_COMPONENT_TYPES)}",
            ))

        comp_id = hyd_schema.next_id(d, "c")
        comp = hyd_schema.new_component(comp_id, type, name=name or type, x=x, y=y, **p)
        if s:
            comp["sourcing"].update(s)

        d["components"].append(comp)
        _HYD["doc"] = d

        errs = hyd_schema.validate_hydraulic(d)
        return _dumps({
            "ok": True,
            "component_id": comp_id,
            "component": comp,
            "doc_component_count": len(d["components"]),
            "warnings": errs,
            "hint": None if not errs else "fix validation errors before proceeding",
            "next_actions": [
                "call hydraulic_add_hose to connect component ports",
                "call hydraulic_size_cylinder / hydraulic_size_pump to size this component",
            ],
        })

    @mcp.tool()
    def hydraulic_add_hose(
        net_name: str,
        connections: Union[str, list],
        doc: Union[str, dict, None] = None,
    ) -> str:
        """Add a hose/line net connecting two or more hydraulic component ports.

        ``net_name`` is a label for the net (e.g. ``"PRESSURE_LINE"``, ``"RETURN_LINE"``).
        ``connections`` is a JSON list of ``{"component_id": "c1", "port": "p1"}`` objects
        identifying which port on which component each end of the hose connects to.

        Port types: pressure, tank, work_a, work_b, drain, generic.

        ``doc`` overrides the session document (optional).

        Returns the updated document with the new hose appended.
        """
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            conns = _as_obj(connections)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        if d is None:
            return _dumps(_err("no active hydraulic circuit",
                               hint="call hydraulic_new first"))
        if not isinstance(conns, list) or len(conns) < 2:
            return _dumps(_err("connections must be a list of >= 2 port references",
                               hint='e.g. [{"component_id":"c1","port":"p1"},{"component_id":"c2","port":"p1"}]'))

        hose_id = hyd_schema.next_id(d, "h")
        hose = hyd_schema.new_hose(hose_id, net_name, conns)
        d["hoses"].append(hose)
        _HYD["doc"] = d

        errs = hyd_schema.validate_hydraulic(d)
        return _dumps({
            "ok": True,
            "hose_id": hose_id,
            "hose": hose,
            "doc_hose_count": len(d["hoses"]),
            "warnings": errs,
            "hint": None if not errs else "fix validation errors before proceeding",
            "next_actions": [
                "call hydraulic_bom to get the sourcing bill of materials",
            ],
        })

    @mcp.tool()
    def hydraulic_size_cylinder(
        bore_mm: float,
        pressure_bar: float,
        stroke_mm: float,
        rod_mm: float = 0.0,
        velocity_mm_s: float = 50.0,
        efficiency: float = 0.95,
    ) -> str:
        """Size a hydraulic cylinder: extend/retract force, flow, and cycle time.

        Physics:
        - Extend force (kN) = bore area (m²) × pressure (Pa) × efficiency / 1000
        - Retract force uses the annulus area (bore² − rod²)
        - Flow (L/min) = area (m²) × velocity (m/s) × 60 × 1000
        - Cycle time (s) = stroke_mm / velocity_mm_s

        Hand-calc check: 50 mm bore, 200 bar, efficiency=1 → ≈ 39.27 kN extend force.

        All inputs in the circuit's native units (mm, bar). Returns a contract-shaped dict
        with all sizing results plus recommended pump flow.
        """
        result = hyd_solve.size_cylinder(
            bore_mm=bore_mm,
            rod_mm=rod_mm,
            pressure_bar=pressure_bar,
            stroke_mm=stroke_mm,
            velocity_mm_s=velocity_mm_s,
            efficiency=efficiency,
        )
        if not result.get("ok"):
            return _dumps(result)
        result.setdefault("warnings", [])
        result.setdefault("hint", (
            f"Pump must supply >= {result['flow_extend_lpm']:.2f} L/min at {pressure_bar} bar. "
            "Call hydraulic_size_pump with these values."
        ))
        return _dumps(result)

    @mcp.tool()
    def hydraulic_size_pump(
        flow_lpm: float,
        pressure_bar: float,
        efficiency: float = 0.85,
        speed_rpm: float = 1500.0,
    ) -> str:
        """Size a hydraulic pump: displacement, hydraulic power, shaft power, torque.

        Physics:
        - Displacement (cc/rev) = Q (L/min) × 1000 / speed (rpm)
        - Hydraulic power (kW) = Q (m³/s) × ΔP (Pa) / 1000
        - Shaft power (kW) = hydraulic power / efficiency
        - Torque (N·m) = shaft power × 1000 / (2π × speed/60)

        Hand-calc check: 10 L/min, 200 bar, η=0.85 → shaft ≈ 3.92 kW.

        Returns displacement, power, and torque in a contract-shaped dict ready to pass to
        component selection.
        """
        result = hyd_solve.size_pump(
            flow_lpm=flow_lpm,
            pressure_bar=pressure_bar,
            efficiency=efficiency,
            speed_rpm=speed_rpm,
        )
        if not result.get("ok"):
            return _dumps(result)
        result.setdefault("warnings", [])
        result.setdefault("hint", (
            f"Select a pump with >= {result['displacement_cc_rev']:.2f} cc/rev displacement "
            f"and motor >= {result['shaft_power_kw']:.2f} kW shaft power."
        ))
        return _dumps(result)

    @mcp.tool()
    def hydraulic_bom(doc: Union[str, dict, None] = None) -> str:
        """Extract a sourcing bill of materials from the active hydraulic circuit.

        Emits VendorPart-compatible sourcing lines for every component in the circuit:
        id, type, name, mpn, manufacturer, description, quantity, unit, category.

        Components without an MPN are included but flagged as un-sourced. A summary
        counts components by type and lists those missing MPNs.

        ``doc`` overrides the session document (optional — uses the last ``hydraulic_new``
        document if omitted).

        Returns a list of BOM lines and a sourcing summary.
        """
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        if d is None:
            return _dumps(_err("no active hydraulic circuit",
                               hint="call hydraulic_new first, or pass doc=<circuit>"))

        errs = hyd_schema.validate_hydraulic(d)
        if errs:
            return _dumps(_err("hydraulic document failed schema validation",
                               hint="fix errors before extracting BOM",
                               errors=errs))

        lines = hyd_bom.hydraulic_bom_lines(d)
        summary = hyd_bom.sourcing_summary(d)
        missing = summary["missing_mpn"]

        return _dumps({
            "ok": True,
            "bom_lines": lines,
            "summary": summary,
            "warnings": [f"component {cid} has no MPN" for cid in missing],
            "hint": (
                "All components sourced." if not missing
                else f"{len(missing)} component(s) missing MPN — add to sourcing fields."
            ),
            "next_actions": [
                "add mpn/manufacturer to unsourced components via hydraulic_add_component sourcing=",
                "pass bom_lines to bom_rollup at B-chain merge time (T-E5 seam)",
            ],
        })
