"""AI-Piping MCP tools — steady-state 1-D pipe-network hydraulic solve + pressure-drop report.

Same design contract as ``ecad_tools`` (project owner: "the AI is extremely limited; its tools
must do the bookkeeping and return rich, actionable context"). Each tool returns a JSON string
``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}``, calls the engine helpers
directly (``engine.piping.solve`` / ``engine.piping.schema``), and holds its own tiny session
state so ``piping_report`` can reuse the document / result of the last ``piping_solve``.

Tools:
  piping_solve    aicad.piping/1 doc + boundary conditions -> node pressures/heads + link flows
  piping_report   per-segment pressure-drop table with an explicit K-factor breakdown

Registration is deferred behind :func:`register_piping_tools` so ``server.py`` wires us in with a
tiny fenced block, exactly like ``register_ecad_tools`` / ``register_cam_tools``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Union

from engine.piping import schema as piping_schema
from engine.piping import solve as piping_solve_mod

# Session bookkeeping: the last solved doc + result so piping_report can be called with no args.
_PIPING: dict = {"doc": None, "result": None, "boundary": None, "fluid": None, "pumps": None}


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def _load_doc(doc: Union[str, dict, None]) -> Any:
    """Accept a piping doc as a dict, a JSON string, or a path to a ``.json`` file."""
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
    """Parse a boundary/fluid/pumps argument that may arrive as a JSON string or native object."""
    if x is None or isinstance(x, (list, dict)):
        return x
    if isinstance(x, str):
        s = x.strip()
        return json.loads(s) if s else None
    return x


def _node_menu(nodes: list) -> list:
    """A compact node reference table so the model can address boundary conditions."""
    return [{"node": n["id"], "xyz": n["xyz"]} for n in nodes]


def register_piping_tools(mcp) -> None:
    """Register the AI-Piping tools on the given FastMCP instance.

    Called from ``mcp_server/server.py`` after ``mcp`` is created. A function (not import-time
    side effects) so the tools can be driven against a stub ``mcp`` in tests without importing the
    whole stdio server — mirrors :func:`register_ecad_tools`.
    """

    @mcp.tool()
    def piping_solve(doc: Union[str, dict], boundary: Union[str, list],
                     fluid: Union[str, dict, None] = None,
                     pumps: Union[str, list, None] = None,
                     network_type: str = "liquid") -> str:
        """Solve an ``aicad.piping/1`` pipe network for node pressures/heads and link flows.

        ``doc`` is the piping document (a dict, a JSON string, or a path to a ``.json`` file) as
        built with the ``engine.piping.schema`` factories (runs + fittings). ``boundary`` is a
        list of per-node conditions — each entry names a node (``{"node":"n0"}`` /
        ``{"at":[x,y,z]}`` / ``{"run":"r1","end":"start"|"end"}``) plus ONE of: ``pressure`` (kPa
        gauge) or ``head`` (m) to fix it, or ``demand`` (m³/h, + leaves / − enters) /
        ``demand_lps``. At least one fixed pressure/head node is required.

        ``fluid`` selects the working fluid: ``null`` (water @20 °C), an explicit
        ``{"rho":kg/m³,"mu":Pa·s}`` override, or a **sourced lookup**
        ``{"name":"water"|"air"|"methane"|"natural_gas"|"nitrogen"|"carbon_dioxide","T":K,"P":Pa}``
        (properties come from CoolProp/thermo if installed, else a cited built-in model — see
        ``piping_fluid``). ``pumps`` is a list of
        ``{"run":"r1","curve":<CSV text/path>,"speed_ratio":1.0,"direction":"a_to_b"}``.

        ``network_type`` = ``"liquid"`` (incompressible, default) or ``"gas"``
        (isothermal-compressible mass-balance solve; needs a named gas ``fluid``, boundary
        pressures are kPa gauge, demands are ``mass_demand`` kg/h or ``std_demand`` Nm³/h, and
        links report ``mass_flow_kgh`` / ``std_flow_nm3h`` / ``dp_kpa``).

        Node ids are derived by snapping coincident run endpoints; the result lists every node
        with its coordinates so you can address boundary conditions. Returns node
        pressures/heads, per-link flow / velocity / Reynolds / friction / pressure-drop, whether
        the solve converged, and the next best action. Never throws for a modelling problem — it
        degrades to ``{"ok": false, "reason", "hint"}``.
        """
        try:
            d = _load_doc(doc)
            bc = _as_obj(boundary)
            fl = _as_obj(fluid)
            pp = _as_obj(pumps)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}",
                               hint="pass doc as JSON/dict/path and boundary as a JSON list"))

        errs = piping_schema.validate(d) if isinstance(d, dict) else ["doc is not an object"]
        if errs:
            return _dumps(_err("piping document failed schema validation",
                               hint="fix the listed errors (see contracts/piping_schema.md)",
                               errors=errs))
        if bc is None:
            return _dumps(_err("boundary conditions are required",
                               hint='e.g. [{"node":"n0","pressure":300},{"node":"n1","demand":50}]'))

        res = piping_solve_mod.solve(d, boundary=bc, fluid=fl, pumps=pp,
                                     network_type=network_type)
        if not res.get("ok"):
            res.setdefault("next_actions", [
                "check every run endpoint meets another run or a boundary node",
                "ensure at least one boundary fixes pressure/head",
            ])
            return _dumps(res)

        _PIPING.update({"doc": d, "result": res, "boundary": bc, "fluid": fl, "pumps": pp})
        res["node_menu"] = _node_menu(res["nodes"])
        return _dumps(res)

    @mcp.tool()
    def piping_report(doc: Union[str, dict, None] = None,
                      boundary: Union[str, list, None] = None,
                      fluid: Union[str, dict, None] = None,
                      pumps: Union[str, list, None] = None) -> str:
        """Per-segment pressure-drop report with an explicit K-factor breakdown.

        With no arguments, reports on the most recent ``piping_solve``. Otherwise pass the same
        ``doc`` + ``boundary`` (+ optional ``fluid``/``pumps``) and a fresh solve is run first.
        Each segment lists the friction K + dP and every fitting's K + dP (elbow/tee/reducer/
        flange), the segment total, and the whole-network ``total_dp_kpa`` — ready to size a pump
        or hand back as the pressure-drop table. Contract-shaped soft-fail like ``piping_solve``.
        """
        try:
            d = _load_doc(doc)
            bc = _as_obj(boundary)
            fl = _as_obj(fluid)
            pp = _as_obj(pumps)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        if d is None:
            if _PIPING.get("result") is None:
                return _dumps(_err("no prior piping_solve to report on",
                                   hint="call piping_solve first, or pass doc + boundary here"))
            rep = piping_solve_mod.pressure_drop_report(
                _PIPING["doc"], _PIPING["result"], fluid=_PIPING["fluid"])
            return _dumps(rep)

        errs = piping_schema.validate(d) if isinstance(d, dict) else ["doc is not an object"]
        if errs:
            return _dumps(_err("piping document failed schema validation", errors=errs))
        if bc is None:
            return _dumps(_err("boundary conditions are required to solve before reporting",
                               hint='pass boundary, e.g. [{"node":"n0","pressure":300}, ...]'))
        rep = piping_solve_mod.pressure_drop_report(d, boundary=bc, fluid=fl, pumps=pp)
        return _dumps(rep)

    @mcp.tool()
    def piping_operating_point(pump_curve: str,
                               system: Union[str, dict, None] = None,
                               doc: Union[str, dict, None] = None,
                               boundary: Union[str, list, None] = None,
                               fluid: Union[str, dict, None] = None,
                               suction: Union[str, dict, None] = None,
                               npshr_curve: Union[str, None] = None,
                               q_ref_m3h: Union[float, None] = None,
                               speed_ratio: float = 1.0) -> str:
        """Pump duty (operating) point + NPSH margin: intersect the pump curve with the system curve.

        ``pump_curve`` — a pump-curve CSV (``flow`` m³/h, ``head`` m) as text or a file path.
        ``system`` — ``{"static_head_m":h0,"k":k}`` giving H_sys = h0 + k·Q² (Q in m³/h); if
        omitted, the system curve is **derived** from ``doc`` + ``boundary`` (one fixed
        pressure/head node + one demand node whose value is the operating flow, or pass
        ``q_ref_m3h``). ``fluid`` as in ``piping_solve`` (used for density + vapour pressure).
        ``suction`` — NPSHa inputs ``{"pressure_kpa":gauge,"elevation_m":z,"friction_m":loss,
        "atm_kpa":101.325,"pvap_kpa":<override>}``. ``npshr_curve`` — a ``flow,npshr`` (m) CSV;
        NPSHr is interpolated at the duty flow to give the NPSH margin (NPSHa − NPSHr).

        Returns ``{"duty_point":{flow_m3h,head_m}, "npsh":{npsha_m,npshr_m,margin_m}, ...}`` or a
        contract-shaped failure (e.g. system head exceeds pump shutoff head → no operating point).
        """
        try:
            sysd = _as_obj(system)
            d = _load_doc(doc)
            bc = _as_obj(boundary)
            fl = _as_obj(fluid)
            suc = _as_obj(suction)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        op = piping_solve_mod.operating_point(
            pump_curve, system=sysd, doc=d, boundary=bc, fluid=fl,
            suction=suc, npshr_curve=npshr_curve, q_ref_m3h=q_ref_m3h,
            speed_ratio=speed_ratio)
        return _dumps(op)

    @mcp.tool()
    def piping_fluid(name: str, T: float = 293.15, P: float = 101325.0) -> str:
        """Sourced thermophysical properties for a named fluid at ``T`` (K), ``P`` (Pa absolute).

        Supported: ``water, air, methane, natural_gas, nitrogen, carbon_dioxide``. Returns density
        (kg/m³), dynamic viscosity (Pa·s), saturation/vapour pressure (Pa; liquids), the backend
        used (CoolProp / thermo / built-in) and a ``source`` citation — the same values the solver
        pulls for ``fluid={"name":...}``. Never fabricates: an unknown fluid or out-of-range
        condition returns ``{"ok": false, "reason", "hint"}``.
        """
        from engine.piping import properties as piping_props
        return _dumps(piping_props.fluid_properties(name, T=float(T), P=float(P)))
