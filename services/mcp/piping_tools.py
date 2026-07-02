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
                     pumps: Union[str, list, None] = None) -> str:
        """Solve an ``aicad.piping/1`` pipe network for node pressures/heads and link flows.

        ``doc`` is the piping document (a dict, a JSON string, or a path to a ``.json`` file) as
        built with the ``engine.piping.schema`` factories (runs + fittings). ``boundary`` is a
        list of per-node conditions — each entry names a node (``{"node":"n0"}`` /
        ``{"at":[x,y,z]}`` / ``{"run":"r1","end":"start"|"end"}``) plus ONE of: ``pressure`` (kPa
        gauge) or ``head`` (m) to fix it, or ``demand`` (m³/h, + leaves / − enters) /
        ``demand_lps``. At least one fixed pressure/head node is required. ``fluid`` overrides the
        default (water @20 °C) with ``{"rho":kg/m³,"mu":Pa·s}``. ``pumps`` is a list of
        ``{"run":"r1","curve":<CSV text/path>,"speed_ratio":1.0,"direction":"a_to_b"}``.

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

        res = piping_solve_mod.solve(d, boundary=bc, fluid=fl, pumps=pp)
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
