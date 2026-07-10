"""AI-Piping MCP tools — steady-state 1-D pipe-network hydraulic solve + pressure-drop report
+ 3D CFD handoff for pipe internals.

Same design contract as ``ecad_tools`` (project owner: "the AI is extremely limited; its tools
must do the bookkeeping and return rich, actionable context"). Each tool returns a JSON string
``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}``, calls the engine helpers
directly (``engine.piping.solve`` / ``engine.piping.schema``), and holds its own tiny session
state so ``piping_report`` can reuse the document / result of the last ``piping_solve``.

Tools:
  piping_solve           aicad.piping/1 doc + boundary conditions -> node pressures/heads + link flows
  piping_report          per-segment pressure-drop table with an explicit K-factor breakdown
  piping_internal_volume extract the internal fluid-domain solid from a pipe run (OCCT boolean)
  piping_cfd_handoff     1-D solve -> extract bore -> invoke run_cfd_flow (LBM) in one step
  piping_verify_cfd      DAG verify node: compare 1D vs 3D pressure drop + flag velocity limits

Registration is deferred behind :func:`register_piping_tools` so ``server.py`` wires us in with a
tiny fenced block, exactly like ``register_ecad_tools`` / ``register_cam_tools``.

CONFORMANCE: ``piping_cfd_handoff`` and ``piping_verify_cfd`` conform to
``aicad.simhandoff/1`` (contracts/sim_handoff.md) — the generalized cross-sim handoff &
verify contract this pair originally proved. Successful results additionally carry a
validated envelope under the reserved ``simhandoff`` key (built through
``services.mcp.simhandoff``); all pre-existing result fields are unchanged.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Union

from engine.piping import schema as piping_schema
from engine.piping import solve as piping_solve_mod
from engine.piping import cfd_handoff as piping_cfd_mod

from services.mcp import simhandoff as sh

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


def _handoff_envelope(solve_res: dict, bc_cfd: dict, step_path: Optional[str],
                      volume_mm3: Optional[float], bore_doc_id: Optional[str]) -> dict:
    """Build the aicad.simhandoff/1 handoff envelope for the piping→CFD coupling.

    Pure translation of already-computed pieces (1D solve, CFD BC extraction, bore
    STEP) into the contract shape — contracts/sim_handoff.md §6 is this envelope.
    Module-level (not a closure) so tests can exercise it engine-less with mock dicts.
    """
    fluid = solve_res.get("fluid") or {}
    total_dp = sum(abs(lk.get("dp_kpa", 0.0)) for lk in solve_res.get("links", []))
    geometry = []
    if step_path:
        geometry.append(sh.geometry_ref(
            "fluid_domain", "step", path=step_path, doc_id=bore_doc_id,
            derived_by="piping_internal_volume", units="mm",
            meta={"volume_mm3": volume_mm3} if volume_mm3 is not None else None))
    conditions = [
        sh.condition("inlet_velocity", "velocity", bc_cfd["inlet_velocity_ms"], "m/s",
                     f"1D solve link {bc_cfd.get('inlet_link')} velocity_ms",
                     applied_to="inlet"),
        sh.condition("inlet_pressure", "pressure", bc_cfd["inlet_pressure_kpa"], "kPa",
                     "1D solve inlet node pressure_kpa", applied_to="inlet"),
        sh.condition("outlet_pressure", "pressure", bc_cfd["outlet_pressure_kpa"], "kPa",
                     f"1D solve node {bc_cfd.get('outlet_node')} pressure_kpa",
                     applied_to="outlet"),
    ]
    expectations = [sh.expectation(
        "pressure_drop", bc_cfd["pressure_drop_1d_kpa"], "kPa",
        "1D inlet−outlet pressure (cfd_boundary_from_solve pressure_drop_1d_kpa)")]
    return sh.build_handoff(
        "piping_to_cfd",
        sh.study_ref("piping", "piping_solve",
                     result_digest={"converged": solve_res.get("converged"),
                                    "total_dp_kpa": round(total_dp, 3),
                                    "fluid": fluid.get("name") or fluid}),
        sh.study_ref("cfd", "run_cfd_flow", doc_id=bore_doc_id),
        geometry=geometry, conditions=conditions, expectations=expectations)


def _verify_envelope(result: dict) -> dict:
    """Build the aicad.simhandoff/1 verify envelope from a verify_pressure_drop result.

    Pure translation — the pass/deviation/verdict numbers are the existing ones,
    re-expressed in the contract's check shape (contracts/sim_handoff.md §6).
    """
    checks = [sh.check("pressure_drop", "kPa",
                       result["dp_1d_kpa"], result["dp_3d_kpa"],
                       result["deviation_pct"], result["tolerance_pct"],
                       result["pass"])]
    viol = (result.get("velocity_limits") or {}).get("violations") or []
    advisories = ([{"name": "velocity_limit", "detail": result["velocity_limits"]}]
                  if viol else None)
    return sh.build_verify(
        "piping_to_cfd",
        sh.study_ref("piping", "piping_solve"),
        sh.study_ref("cfd", "run_cfd_flow"),
        checks, result.get("verdict", ""),
        advisories=advisories,
        warnings=result.get("warnings") or None)


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

    @mcp.tool()
    def piping_internal_volume(doc: Union[str, dict],
                               out_path: Optional[str] = None,
                               run_id: Optional[str] = None) -> str:
        """Extract the internal fluid-domain solid (bore) from a pipe run and write it as STEP.

        The STEP file is a watertight solid of the fluid passage — suitable for import as a
        CAD part and then handing to ``run_cfd_flow`` / ``run_cfd_accurate`` for internal-flow
        CFD. The bore solid is the swept inner-diameter circle along the centreline (the void
        that the fluid occupies), not the pipe wall.

        ``doc`` is the piping document (dict, JSON string, or path). ``out_path`` is the
        destination STEP file; if omitted a temporary file is created. ``run_id`` restricts
        extraction to one named run (default: all non-suppressed runs).

        Returns ``{ok, step_path, run_ids, volume_mm3, inlet_xyz, outlet_xyz, warnings,
        next_actions}``. Soft-fails with ``{ok: false, reason, hint}`` when the kernel is
        absent or no geometry can be produced — never a bare traceback.
        """
        try:
            d = _load_doc(doc)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse doc: {exc}",
                               hint="pass doc as a dict, JSON string, or file path"))

        errs = piping_schema.validate(d) if isinstance(d, dict) else ["doc is not an object"]
        if errs:
            return _dumps(_err("piping document failed schema validation",
                               hint="fix the listed errors", errors=errs))

        if out_path is None:
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="piping_bore_")
            out_path = os.path.join(tmp_dir, "fluid_domain.step")

        try:
            result = piping_cfd_mod.internal_volume(d, out_path, run_id=run_id)
        except piping_cfd_mod.PipingCFDError as exc:
            return _dumps(_err(str(exc),
                               hint="install build123d (pip install build123d) or run on a "
                                    "machine with the OCP kernel"))
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"internal_volume failed: {exc}"))

        result.setdefault("next_actions", [
            "import the step_path as a CAD part with import_part(step_path)",
            "run run_cfd_flow on the imported part for internal-flow LBM",
            "or run piping_cfd_handoff to do solve + bore extraction + CFD in one step",
        ])
        return _dumps(result)

    @mcp.tool()
    def piping_cfd_handoff(doc: Union[str, dict],
                           boundary: Union[str, list],
                           fluid: Union[str, dict, None] = None,
                           pumps: Union[str, list, None] = None,
                           run_id: Optional[str] = None,
                           cells: int = 96,
                           n_steps: int = 2000,
                           background: bool = False) -> str:
        """One-click 3D CFD handoff for pipe internals.

        Runs the full pipeline in one call:
        1. Solves the 1D pipe network (same as ``piping_solve``).
        2. Extracts the internal fluid-domain bore solid as STEP (same as
           ``piping_internal_volume``) — the watertight void the fluid occupies.
        3. Derives CFD inlet/outlet boundary conditions from the 1D result
           (inlet velocity, inlet/outlet pressures).
        4. Imports the bore STEP as a CAD part and invokes ``run_cfd_flow`` (LBM) with the
           1D-derived boundary conditions.

        ``run_cfd_accurate`` (OpenFOAM RANS) is NOT invoked here because it requires
        OpenFOAM to be installed; use ``piping_verify_cfd`` after a manual
        ``run_cfd_accurate`` call if you need RANS accuracy. The LBM path is always
        available and gives fast directional results for comparing configurations.

        ``run_id`` restricts the bore extraction to one named run; if omitted all runs are
        joined. ``cells`` / ``n_steps`` / ``background`` are forwarded to ``run_cfd_flow``.

        Returns ``{ok, solve, boundary_cfd, step_path, cfd, next_actions}`` — all sub-results
        included so ``piping_verify_cfd`` can be called immediately on the returned data.
        Successful results also carry a validated ``aicad.simhandoff/1`` handoff envelope
        under ``simhandoff`` (contracts/sim_handoff.md).
        """
        # ── 1D solve ──────────────────────────────────────────────────────────
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
                               errors=errs))
        if bc is None:
            return _dumps(_err("boundary conditions are required",
                               hint='e.g. [{"node":"n0","pressure":300},{"node":"n1","demand":50}]'))

        solve_res = piping_solve_mod.solve(d, boundary=bc, fluid=fl, pumps=pp)
        if not solve_res.get("ok"):
            solve_res.setdefault("next_actions", [
                "check every run endpoint meets another run or a boundary node",
                "ensure at least one boundary fixes pressure/head",
            ])
            return _dumps({"ok": False, "stage": "1d_solve",
                           "reason": solve_res.get("reason", "1D solve failed"),
                           "solve": solve_res})

        _PIPING.update({"doc": d, "result": solve_res, "boundary": bc, "fluid": fl, "pumps": pp})

        # ── extract CFD BCs from 1D result ────────────────────────────────────
        bc_cfd = piping_cfd_mod.cfd_boundary_from_solve(solve_res, inlet_run_id=run_id)
        if not bc_cfd.get("ok"):
            return _dumps({"ok": False, "stage": "bc_extraction",
                           "reason": bc_cfd.get("reason"),
                           "solve": solve_res, "boundary_cfd": bc_cfd})

        inlet_v_ms = bc_cfd["inlet_velocity_ms"]

        # ── extract bore solid ────────────────────────────────────────────────
        import tempfile
        tmp_dir = tempfile.mkdtemp(prefix="piping_cfd_")
        step_path = os.path.join(tmp_dir, "fluid_domain.step")

        try:
            vol_res = piping_cfd_mod.internal_volume(d, step_path, run_id=run_id)
        except piping_cfd_mod.PipingCFDError as exc:
            # Kernel absent: return solve + BCs, note CFD skipped, never traceback
            return _dumps({
                "ok": False,
                "stage": "bore_extraction",
                "reason": str(exc),
                "hint": ("build123d/OCP kernel not available — install build123d to extract "
                         "the bore solid for CFD. The 1D solve and CFD BCs are still valid."),
                "solve": solve_res,
                "boundary_cfd": bc_cfd,
                "cfd": None,
            })
        except Exception as exc:  # noqa: BLE001
            return _dumps({
                "ok": False, "stage": "bore_extraction",
                "reason": f"internal_volume failed: {exc}",
                "solve": solve_res, "boundary_cfd": bc_cfd, "cfd": None,
            })

        # ── import bore STEP as CAD part + invoke run_cfd_flow ────────────────
        # We invoke run_cfd_flow through the MCP bridge (core_bridge) if available;
        # otherwise we record the step_path and BCs so the user can invoke CFD manually.
        try:
            from services.mcp.core_bridge import CoreBridge
            cb = CoreBridge()
            # Import the bore STEP as a new part
            imp = cb.call("import_file", {"path": step_path, "format": "step"})
            bore_doc_id = imp.get("doc_id") if isinstance(imp, dict) else None
        except Exception:  # noqa: BLE001
            bore_doc_id = None

        if bore_doc_id is None:
            # Core bridge not available or import failed — return the STEP + BCs,
            # instruct the caller to import manually then run run_cfd_flow.
            return _dumps(sh.attach({
                "ok": True,
                "stage": "cfd_pending_manual_import",
                "solve": solve_res,
                "boundary_cfd": bc_cfd,
                "step_path": step_path,
                "volume_mm3": vol_res.get("volume_mm3"),
                "cfd": None,
                "next_actions": [
                    f"import_part('{step_path}') to load the bore solid into the CAD session",
                    f"run_cfd_flow(velocity={inlet_v_ms}, fluid='water', cells={cells}) "
                    "on the imported bore part",
                    "then call piping_verify_cfd(solve_result, cfd_result) to compare 1D vs 3D dP",
                ],
                "hint": (
                    "Core bridge not available in this context — import the bore STEP manually "
                    "and call run_cfd_flow. All BCs are in boundary_cfd."
                ),
            }, _handoff_envelope(solve_res, bc_cfd, step_path,
                                 vol_res.get("volume_mm3"), None)))

        # Run CFD on the imported bore part
        try:
            # Call via core_bridge sim_run_flow (what run_cfd_flow uses internally).
            # NB: never `from services.mcp import cad_tools` here — every other tool
            # module imports it as `mcp_server.cad_tools`, and a second module identity
            # would mean a second _PERIODIC_TIMER (the autosave thread-leak class).
            prog_path = os.path.join(tmp_dir, "progress.json")
            cfd_raw = cb.call("sim_run_flow", {
                "doc_id": bore_doc_id,
                "velocity": float(inlet_v_ms) if inlet_v_ms > 0 else 1.0,
                "aoa": 0.0,
                "fluid": "water",
                "cells": cells,
                "n_steps": n_steps,
                "progress_path": prog_path,
            })
            cfd_res = {"ok": cfd_raw.get("ok", False)}
            for k in ("drag_n", "lift_n", "cd", "frontal_area_mm2", "reynolds",
                      "ref_length_mm", "velocity_ms", "aoa_deg", "fluid", "grid",
                      "converged", "residual", "voxel_mm", "min_feature_mm",
                      "min_feature_voxels", "steps_run", "diverged", "warning"):
                if k in cfd_raw:
                    cfd_res[k] = cfd_raw[k]
        except Exception as exc:  # noqa: BLE001
            cfd_res = {"ok": False, "reason": f"run_cfd_flow failed: {exc}"}

        return _dumps(sh.attach({
            "ok": True,
            "solve": solve_res,
            "boundary_cfd": bc_cfd,
            "step_path": step_path,
            "volume_mm3": vol_res.get("volume_mm3"),
            "cfd": cfd_res,
            "next_actions": [
                "call piping_verify_cfd(solve_result=<solve>, cfd_result=<cfd>) "
                "to compare 1D vs 3D pressure drop and check velocity limits",
                "raise cells/n_steps for more accurate CFD (at cost of runtime)",
                "use run_cfd_accurate for RANS-quality results (requires OpenFOAM)",
            ],
        }, _handoff_envelope(solve_res, bc_cfd, step_path,
                             vol_res.get("volume_mm3"), bore_doc_id)))

    @mcp.tool()
    def piping_verify_cfd(solve_result: Union[str, dict],
                          cfd_result: Union[str, dict],
                          tolerance_pct: float = 20.0,
                          run_id: Optional[str] = None) -> str:
        """DAG verify node: compare 1D pipe-network pressure drop with 3D CFD pressure drop.

        Takes the output of ``piping_solve`` (or the ``solve`` field from
        ``piping_cfd_handoff``) and the output of ``run_cfd_flow`` / ``run_cfd_accurate``
        (or the ``cfd`` field from ``piping_cfd_handoff``) and:

        * Computes the 1D pressure drop budget (sum of all link dp_kpa, or a single
          run when ``run_id`` is given).
        * Derives the CFD pressure drop from ``drag_n`` / ``frontal_area_mm2``
          (or falls back to Cd × ½ρV²).
        * Flags PASS / FAIL at the given ``tolerance_pct`` (default 20 %).
        * Checks every pipe link for velocity limits (> 3 m/s = erosion advisory for
          liquid service).

        If called with no live results, falls back to the most recent ``piping_cfd_handoff``
        result stored in session state. Always returns ``{ok, pass, dp_1d_kpa, dp_3d_kpa,
        deviation_pct, tolerance_pct, verdict, velocity_limits, warnings}``. Successful
        results also carry a validated ``aicad.simhandoff/1`` verify envelope under
        ``simhandoff`` (contracts/sim_handoff.md).
        """
        try:
            sr = _as_obj(solve_result)
            cr = _as_obj(cfd_result)
        except (ValueError, TypeError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}",
                               hint="pass solve_result and cfd_result as dicts or JSON strings"))

        if sr is None:
            if _PIPING.get("result") is None:
                return _dumps(_err("no prior piping_solve result in session",
                                   hint="call piping_solve or piping_cfd_handoff first"))
            sr = _PIPING["result"]

        if cr is None:
            return _dumps(_err("cfd_result is required",
                               hint="pass the result from run_cfd_flow or piping_cfd_handoff['cfd']"))

        result = piping_cfd_mod.verify_pressure_drop(
            sr, cr, tolerance_pct=float(tolerance_pct), run_id=run_id
        )
        if result.get("ok"):
            sh.attach(result, _verify_envelope(result))
        return _dumps(result)
