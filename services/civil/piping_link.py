"""Civil/process interfaces and evidence envelopes (aicad.civil/1 §7).

Hydrostatic conversion uses p = rho*g*(H-z); fixed-point coupling uses relaxed
Picard iteration and nodal continuity. References: EPA EPANET 2.2 User Manual,
§12 hydraulic equations; contracts/piping_schema.md and contracts/sim_handoff.md.
No daemon is imported or called: the process-network solver is injected.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from typing import Callable

from services.mcp import simhandoff as sh
from .errors import CivilError

_G = 9.80665
_WATER = {"name": "water", "rho": 1000.0, "mu": 0.001004, "phase": "liquid"}
_METHOD = {"name": "Under-relaxed hydraulic fixed-point coupling",
           "reference": "EPA EPANET 2.2 User Manual §12; aicad.civil/1 §7; nodal continuity and p=rho*g*(H-z)"}
_DEFAULT_ASSUMPTION = "Freshwater idealization: density 1000 kg/m3 and dynamic viscosity 0.001004 Pa*s when fluid is omitted."


def _number(value, label, *, positive=False):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or (positive and value <= 0)):
        raise CivilError("invalid_input", f"{label} must be finite" + (" and positive" if positive else ""))
    return float(value)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise CivilError("invalid_input", f"{label} must be a nonempty string")
    return value


def _json(value, label, *, solver_result=False):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        reason = "piping_solver_failed" if solver_result else "invalid_input"
        extra = {"solver_reason": "invalid_result"} if solver_result else {}
        raise CivilError(reason, f"{label} must contain only finite JSON data", **extra) from exc


def _network(model, network_id):
    if not isinstance(model, dict):
        raise CivilError("invalid_input", "model must be an aicad.civil/1 dictionary")
    for network in model.get("water_networks", []):
        if network.get("id") == network_id:
            return network
    raise CivilError("tie_in_network_mismatch", "Tie-in must reference an existing water network", network_id=network_id)


def _junction(network, node_id):
    for node in network.get("junctions") or []:
        if node.get("id") == node_id:
            return node
    raise CivilError("tie_in_not_found", "civil_node must reference a water junction", civil_node=node_id)


def _ties(model, network_id=None, tie_in_ids=None):
    if not isinstance(model, dict) or not isinstance(model.get("tie_ins", []), list):
        raise CivilError("invalid_input", "model.tie_ins must be a list")
    ties = model.get("tie_ins", [])
    if tie_in_ids is not None:
        if (not isinstance(tie_in_ids, list) or not tie_in_ids
                or any(not isinstance(t, str) for t in tie_in_ids)
                or len(set(tie_in_ids)) != len(tie_in_ids)):
            raise CivilError("invalid_input", "tie_in_ids must be a nonempty list of unique ids")
        indexed = {t.get("id"): t for t in ties if isinstance(t, dict)}
        missing = [t for t in tie_in_ids if t not in indexed]
        if missing:
            raise CivilError("tie_in_not_found", "Requested tie-in does not exist", tie_in_ids=missing)
        ties = [indexed[t] for t in tie_in_ids]
        if any(t.get("civil_network") != network_id for t in ties):
            raise CivilError("tie_in_network_mismatch", "Selected tie-ins belong to another civil network", network_id=network_id)
    elif network_id is not None:
        ties = [t for t in ties if isinstance(t, dict) and t.get("civil_network") == network_id]
    if not ties:
        reason = "tie_in_network_mismatch" if model.get("tie_ins") and network_id is not None else "tie_in_not_found"
        raise CivilError(reason, "No tie-ins selected for the requested network")
    seen = set()
    for tie in ties:
        if not isinstance(tie, dict):
            raise CivilError("invalid_input", "Each tie-in must be a dictionary")
        tid = _text(tie.get("id"), "tie_in.id")
        if tid in seen:
            raise CivilError("invalid_input", "Duplicate tie-in id", tie_in_id=tid)
        seen.add(tid)
        for key in ("civil_network", "civil_node", "piping_doc_id"):
            _text(tie.get(key), f"tie_in.{key}")
        endpoint = tie.get("piping_endpoint")
        if not isinstance(endpoint, dict):
            raise CivilError("invalid_input", "piping_endpoint must be a run_id/end dictionary")
        _text(endpoint.get("run_id"), "piping_endpoint.run_id")
        if endpoint.get("end") not in ("start", "end"):
            raise CivilError("invalid_input", "piping_endpoint.end must be start or end")
        if tie.get("direction") not in ("supply", "discharge"):
            raise CivilError("invalid_input", "tie_in.direction must be supply or discharge")
        _number(tie.get("elevation_offset_m", 0.0), "elevation_offset_m")
        _junction(_network(model, tie["civil_network"]), tie["civil_node"])
    return ties


def _boundary_rows(model, water_result, ties, timestep, rho):
    if (not isinstance(timestep, int) or isinstance(timestep, bool) or timestep < 0
            or not isinstance(water_result, dict)):
        raise CivilError("invalid_input", "timestep must be a nonnegative integer and water_result a dictionary")
    steps = water_result.get("timesteps")
    if not isinstance(steps, list) or timestep >= len(steps):
        raise CivilError("timestep_not_found", "Requested hydraulic timestep does not exist", timestep=timestep)
    if water_result.get("ok") is not True or water_result.get("converged") is not True:
        raise CivilError("civil_solver_failed", "Boundaries require a converged civil hydraulic result")
    nodes = steps[timestep].get("nodes", {})
    out = {}
    for tie in ties:
        network_id = tie["civil_network"]
        reported_network = water_result.get("network_id")
        if reported_network is not None and reported_network != network_id:
            raise CivilError("tie_in_network_mismatch", "water_result belongs to a different network")
        node = _junction(_network(model, network_id), tie["civil_node"])
        solved = nodes.get(tie["civil_node"])
        if not isinstance(solved, dict):
            raise CivilError("tie_in_not_found", "civil_node is absent from water_result", tie_in_id=tie["id"])
        elevation = _number(node.get("elevation"), "junction.elevation") + tie.get("elevation_offset_m", 0.0)
        head = _number(solved.get("head_m"), "water_result.head_m")
        pressure = rho * _G * (head - elevation) / 1000.0
        _number(pressure, "tie-in pressure_kpa")
        endpoint = tie["piping_endpoint"]
        out[tie["id"]] = {"civil_network": network_id, "civil_node": tie["civil_node"],
                           "piping_doc_id": tie["piping_doc_id"], "direction": tie["direction"],
                           "head_m": head, "pressure_kpa": pressure,
                           "elevation_m": elevation, "elevation_mm": elevation * 1000.0,
                           "boundary": {"run": endpoint["run_id"], "end": endpoint["end"], "pressure": pressure}}
    return out


def tie_in_boundaries(model: dict, water_result: dict, timestep: int = 0) -> dict:
    """Map civil heads to native run/end pressure BCs (kPa gauge, not bar/Pa).

    Hydrostatics p=rho*g*(H-z), EPANET 2.2 Manual §12. Endpoint elevation is
    reported in mm as well as m; the native pressure BC deliberately uses only
    run/end (adding an ``at`` would violate the solver's one-reference rule).
    Density may be supplied as water_result.density_kgm3; omitted density uses
    the stated freshwater idealization, never the core's implicit fluid default.
    """
    ties = _ties(model)
    if len({t["civil_network"] for t in ties}) != 1:
        raise CivilError("tie_in_network_mismatch", "One water_result cannot supply boundaries for multiple civil networks")
    if not isinstance(water_result, dict):
        raise CivilError("invalid_input", "water_result must be a dictionary")
    rho = _number(water_result.get("density_kgm3", _WATER["rho"]), "density_kgm3", positive=True)
    rows = _boundary_rows(model, water_result, ties, timestep, rho)
    assumptions = [] if "density_kgm3" in water_result else [_DEFAULT_ASSUMPTION]
    return {"ok": True, "tie_ins": rows,
            "boundary": [row["boundary"] for row in rows.values()],
            "density_kgm3": rho, "warnings": [], "assumptions": assumptions,
            "method": {"name": "Hydrostatic tie-in boundary conversion",
                       "reference": "p=rho*g*(H-z); EPA EPANET 2.2 Manual §12; native piping pressure in kPa gauge"}}


def _inputs(piping_inputs, ties):
    if not isinstance(piping_inputs, dict):
        raise CivilError("invalid_input", "piping_inputs must be a dictionary")
    _json(piping_inputs, "piping_inputs")
    doc_ids = list(dict.fromkeys(t["piping_doc_id"] for t in ties))
    if "doc" in piping_inputs:
        if len(doc_ids) != 1:
            raise CivilError("tie_in_network_mismatch", "Use a piping_doc_id-keyed input mapping for multiple documents")
        entries = {doc_ids[0]: deepcopy(piping_inputs)}
    else:
        entries = {}
        for doc_id in doc_ids:
            if not isinstance(piping_inputs.get(doc_id), dict):
                raise CivilError("tie_in_not_found", "Missing piping input document", piping_doc_id=doc_id)
            entries[doc_id] = deepcopy(piping_inputs[doc_id])
    assumptions = []
    for doc_id, entry in entries.items():
        doc = entry.get("doc")
        if not isinstance(doc, dict) or doc.get("schema") != "aicad.piping/1":
            raise CivilError("invalid_input", "Each piping input needs an aicad.piping/1 doc")
        if entry.get("network_type", "liquid") != "liquid":
            raise CivilError("unsupported_fluid", "Civil water coupling requires incompressible liquid piping")
        fluid = entry.get("fluid")
        if fluid is None:
            fluid = deepcopy(_WATER)
            assumptions.append(_DEFAULT_ASSUMPTION)
        if not isinstance(fluid, dict) or "rho" not in fluid or "mu" not in fluid:
            raise CivilError("fluid_properties_required", "Resolve named fluid properties before coupling; supply rho (kg/m3) and mu (Pa*s)")
        _number(fluid["rho"], "fluid.rho", positive=True)
        _number(fluid["mu"], "fluid.mu", positive=True)
        if fluid.get("phase", "liquid") != "liquid":
            raise CivilError("unsupported_fluid", "Civil water coupling requires liquid fluid")
        entry["fluid"] = fluid
        boundary = entry.get("boundary", [])
        if isinstance(boundary, dict):
            boundary = boundary.get("nodes")
        if not isinstance(boundary, list) or any(not isinstance(b, dict) for b in boundary):
            raise CivilError("invalid_input", "piping_inputs.boundary must be a list of conditions")
        entry["boundary"] = boundary
        endpoints = set()
        for tie in (t for t in ties if t["piping_doc_id"] == doc_id):
            endpoint = tie["piping_endpoint"]
            key = (endpoint["run_id"], endpoint["end"])
            if key in endpoints:
                raise CivilError("duplicate_tie_in_endpoint", "An endpoint cannot represent two independent civil exchanges")
            endpoints.add(key)
            run = next((r for r in doc.get("runs", []) if r.get("id") == key[0] and not r.get("suppressed")), None)
            if run is None or len(run.get("path", [])) < 2:
                raise CivilError("tie_in_not_found", "piping_endpoint references no active run", tie_in_id=tie["id"])
            # A second pressure/demand on the same snapped endpoint is ambiguous;
            # generated civil BCs never silently override the caller's plant BCs.
            xyz = run["path"][0 if key[1] == "start" else -1]
            for bc in boundary:
                if ((bc.get("run"), bc.get("end", "start")) == key
                        or bc.get("at") == xyz):
                    raise CivilError("conflicting_tie_in_boundary", "Remove the plant boundary at the civil-coupled endpoint", tie_in_id=tie["id"])
    densities = {entry["fluid"]["rho"] for entry in entries.values()}
    if len(densities) != 1:
        raise CivilError("tie_in_fluid_mismatch", "All tie-ins on a civil water network must use the same density")
    return entries, float(next(iter(densities))), list(dict.fromkeys(assumptions))


def _exchange(result, tie, row, rho):
    links = result.get("links")
    nodes = result.get("nodes")
    if (not isinstance(links, list) or not isinstance(nodes, list)
            or any(not isinstance(v, dict) for v in [*links, *nodes])):
        raise CivilError("piping_solver_failed", "Expected native nodes/links lists of objects", solver_reason="invalid_result")
    endpoint = tie["piping_endpoint"]
    link = next((l for l in links if l.get("id") == endpoint["run_id"]), None)
    if link is None:
        raise CivilError("piping_solver_failed", "No solved link at piping_endpoint", solver_reason="tie_in_not_found")
    node_id = link.get("from" if endpoint["end"] == "start" else "to")
    node = next((n for n in nodes if n.get("id") == node_id), None)
    if node is None:
        raise CivilError("piping_solver_failed", "No solved node at piping_endpoint", solver_reason="tie_in_not_found")
    # The boundary supplies the entire snapped node, not just the selected run.
    # Continuity: civil withdrawal = sum(outgoing links)-sum(incoming)+plant demand.
    outward = _number(node.get("demand_m3h", 0.0), "piping node demand_m3h") / 3600.0
    for incident in links:
        if incident.get("from") == node_id or incident.get("to") == node_id:
            q = _number(incident.get("flow_m3h"), "piping link flow_m3h") / 3600.0
            outward += q * (int(incident.get("from") == node_id) - int(incident.get("to") == node_id))
    pressure = node.get("pressure_kpa")
    if pressure is None:
        xyz = node.get("xyz")
        if not isinstance(xyz, list) or len(xyz) != 3:
            raise CivilError("piping_solver_failed", "Need endpoint pressure_kpa or head_m and xyz", solver_reason="invalid_result")
        pressure = rho * _G * (_number(node.get("head_m"), "piping node head_m") - _number(xyz[2], "piping node z_mm") / 1000.0) / 1000.0
    pressure = _number(pressure, "piping node pressure_kpa")
    return {"flow_m3s": outward if tie["direction"] == "supply" else -outward,
            "civil_demand_m3s": outward,
            "piping_head_m": row["elevation_m"] + pressure * 1000.0 / (rho * _G),
            "piping_pressure_kpa": pressure}


def _relative(a, b):
    # Symmetric relative error: zero/zero is exactly zero; zero/nonzero is one.
    # This convention avoids infinite JSON percentages and is declared in envelopes.
    denominator = max(abs(a), abs(b))
    return abs(a - b) / denominator if denominator else 0.0


def _validated(envelope):
    errors = sh.validate_envelope(envelope)
    if errors:
        raise CivilError("invalid_handoff", "Generated envelope violates aicad.simhandoff/1", errors=errors)
    return envelope


def _envelopes(network_id, ties, rows, entries, applied, converged, tol_rel, warnings):
    civil = sh.study_ref("civil", "civil_water_solve", study=network_id,
                         result_digest={"tie_ins": {t["id"]: {"head_m": rows[t["id"]]["head_m"]} for t in ties}})
    doc_ids = list(entries)
    piping = sh.study_ref("piping", "piping_solve", doc_id=doc_ids[0] if len(doc_ids) == 1 else None,
                          study=ties[0].get("piping_study", "piping_network"))
    conditions = []
    flow_conditions = []
    checks = []
    for tie in ties:
        tid, row = tie["id"], rows[tie["id"]]
        endpoint = tie["piping_endpoint"]
        target = f"{tie['piping_doc_id']}:{endpoint['run_id']}:{endpoint['end']}"
        conditions.append(sh.condition(f"{tid}_pressure", "pressure", row["pressure_kpa"], "kPa",
                                       f"civil water node {tie['civil_node']} head_m minus elevation and elevation_offset_m, multiplied by rho*g/1000",
                                       applied_to=target))
        flow_conditions.append(sh.condition(f"{tid}_flow", "flow", row["civil_demand_m3s"], "m3/s",
                                            f"piping node continuity at {target}; incident flow_m3h and demand_m3h divided by 3600; positive civil withdrawal, negative inflow",
                                            applied_to=f"{network_id}:{tie['civil_node']}"))
        for quantity, units, source, target_value in (
                (f"head:{tid}", "m", row["head_m"], row["piping_head_m"]),
                (f"flow:{tid}", "m3/s", applied[tid], row["civil_demand_m3s"])):
            deviation = _relative(source, target_value) * 100.0
            checks.append(sh.check(quantity, units, source, target_value, deviation, tol_rel * 100.0, deviation <= tol_rel * 100.0))
    to_piping = _validated(sh.build_handoff(
        "civil_to_piping", civil, piping,
        geometry=[sh.geometry_ref("doc", "doc", inline=e["doc"], derived_by="civil_tie_in", units="mm", meta={"piping_doc_id": d}) for d, e in entries.items()],
        conditions=conditions, warnings=warnings,
        meta={"network_id": network_id, "tie_ins": rows, "piping_inputs": entries,
              **({"boundary": entries[doc_ids[0]]["boundary"], "fluid": entries[doc_ids[0]]["fluid"]} if len(doc_ids) == 1 else {})}))
    piping = sh.study_ref("piping", "piping_solve", doc_id=piping.get("doc_id"), study=piping["study"], handoff_id=to_piping["id"])
    to_civil = _validated(sh.build_handoff(
        "piping_to_civil", piping, civil, conditions=flow_conditions, warnings=warnings,
        meta={"network_id": network_id, "flow_sign": "positive withdrawal; negative civil inflow"}))
    verify = _validated(sh.build_verify(
        "civil_to_piping", civil, piping, checks,
        ("PASS" if all(c["pass"] for c in checks) else "FAIL") + ": final tie-in head and exchanged flow agreement; convergence stability reported separately.",
        handoff_id=to_piping["id"], warnings=warnings,
        advisories=[{"name": "relative_error_definition", "detail": "abs(a-b)/max(abs(a),abs(b)); zero/zero=0"},
                    {"name": "iteration_stability", "detail": {"converged": converged}}]))
    return {"handoff_civil_to_piping": to_piping, "handoff_piping_to_civil": to_civil, "verify": verify}


def couple(model: dict, network_id: str, piping_solver: Callable, *, piping_inputs: dict,
           tol_rel: float = 0.01, max_iter: int = 30, relax: float = 0.5) -> dict:
    """Solve civil supply/plant demand intersection by relaxed Picard iteration.

    EPA EPANET 2.2 Manual §12 (headloss/continuity). ``piping_inputs`` is either
    {doc, boundary, fluid} for one piping_doc_id, or a document-id-keyed mapping
    of those entries. Optional top-level tie_in_ids explicitly selects interfaces.
    The callable receives (doc, boundary, fluid) and returns native nodes/links.
    No caller model or inputs are mutated. DDA, duration=0 only: independent
    pressure-demand scaling and time integration require a different coupled model.
    Convergence requires head/flow iteration change AND actual exchange agreement
    below tol_rel, not merely small under-relaxed updates. Reverse flow is signed.
    """
    tol_rel = _number(tol_rel, "tol_rel", positive=True)
    relax = _number(relax, "relax", positive=True)
    if tol_rel >= 1 or relax > 1 or not isinstance(max_iter, int) or isinstance(max_iter, bool) or max_iter < 1:
        raise CivilError("invalid_input", "Require 0<tol_rel<1, 0<relax<=1 and integer max_iter>=1")
    if not callable(piping_solver):
        raise CivilError("invalid_input", "piping_solver must be callable")
    network = deepcopy(_network(model, network_id))
    options = network.get("options") or {}
    if not isinstance(options, dict):
        raise CivilError("invalid_input", "water network options must be a dictionary")
    if options.get("demand_multiplier", 1.0) != 1.0:
        raise CivilError("unsupported_tie_in_pattern", "Demand multipliers would rescale the plant exchange; use an unscaled tie-in network")
    if options.get("duration_s", 0) != 0:
        raise CivilError("coupling_requires_steady_state", "Couple steady-state snapshots; an EPS plant demand needs time-dependent coupling")
    if options.get("demand_model", "DDA") != "DDA":
        raise CivilError("unsupported_demand_model", "Use DDA; the plant solver already determines its pressure-dependent demand")
    ties = _ties(model, network_id, piping_inputs.get("tie_in_ids") if isinstance(piping_inputs, dict) else None)
    entries, rho, assumptions = _inputs(piping_inputs, ties)
    assumptions += ["Steady incompressible coupling; civil base junction demands are preserved and plant exchanges are additive.",
                    "Relative deviations use abs(a-b)/max(abs(a),abs(b)); zero/zero=0."]
    base_demands = {t["civil_node"]: _number(_junction(network, t["civil_node"]).get("demand_m3s", 0.0), "junction.demand_m3s") for t in ties}
    if any(_junction(network, t["civil_node"]).get("pattern") is not None for t in ties):
        raise CivilError("unsupported_tie_in_pattern", "Tie-in junction demand must be unpatterned so imposed exchange remains unscaled")
    applied = {t["id"]: 0.0 for t in ties}
    previous = None
    history = []
    warnings = []
    converged = False
    from . import water  # solver import stays lazy: no native engine/daemon dependency
    for iteration in range(1, max_iter + 1):
        for node_id, base in base_demands.items():
            _junction(network, node_id)["demand_m3s"] = base + sum(applied[t["id"]] for t in ties if t["civil_node"] == node_id)
        civil_result = water.solve(network, criteria=model.get("criteria"))
        rows = _boundary_rows(model, civil_result, ties, 0, rho)
        solved_entries = deepcopy(entries)
        piping_results = {}
        for doc_id, entry in solved_entries.items():
            entry["boundary"] += [rows[t["id"]]["boundary"] for t in ties if t["piping_doc_id"] == doc_id]
            try:
                result = piping_solver(deepcopy(entry["doc"]), deepcopy(entry["boundary"]), deepcopy(entry["fluid"]))
            except Exception as exc:
                raise CivilError("piping_solver_failed", str(exc), solver_reason=getattr(exc, "reason", type(exc).__name__)) from exc
            if not isinstance(result, dict) or result.get("ok") is not True or result.get("converged") is False:
                reason = result.get("reason", "not_converged" if result.get("converged") is False else "invalid_result") if isinstance(result, dict) else "invalid_result"
                raise CivilError("piping_solver_failed", "Injected piping solver refused or failed", solver_reason=reason)
            _json(result, "piping result", solver_result=True)
            piping_results[doc_id] = result
        for tie in ties:
            tid = tie["id"]
            try:
                rows[tid].update(_exchange(piping_results[tie["piping_doc_id"]], tie, rows[tid], rho))
            except CivilError as exc:
                if exc.reason == "piping_solver_failed":
                    raise
                raise CivilError("piping_solver_failed", exc.hint, solver_reason=exc.reason) from exc
            rows[tid]["civil_flow_m3s"] = applied[tid] if tie["direction"] == "supply" else -applied[tid]
        head_change = max(_relative(rows[t]["head_m"], previous[t]["head_m"]) for t in rows) if previous is not None else None
        flow_change = max(_relative(rows[t]["flow_m3s"], previous[t]["flow_m3s"]) for t in rows) if previous is not None else None
        exchange_error = max(_relative(applied[t], rows[t]["civil_demand_m3s"]) for t in rows)
        head_error = max(_relative(rows[t]["head_m"], rows[t]["piping_head_m"]) for t in rows)
        history.append({"iteration": iteration, "head_change_rel": head_change, "flow_change_rel": flow_change,
                        "flow_exchange_residual_rel": exchange_error, "head_agreement_rel": head_error,
                        "tie_ins": deepcopy(rows)})
        converged = (previous is not None and head_change < tol_rel and flow_change < tol_rel
                     and exchange_error < tol_rel and head_error < tol_rel)
        if converged or iteration == max_iter:
            break
        previous = rows
        applied = {tid: applied[tid] + relax * (row["civil_demand_m3s"] - applied[tid]) for tid, row in rows.items()}
    if not converged:
        warnings.append("Coupled iteration did not converge; final results and agreement are reported without a convergence claim.")
    for tie in ties:
        if rows[tie["id"]]["flow_m3s"] < 0:
            warnings.append(f"{tie['id']}: flow is opposite to the declared {tie['direction']} direction.")
    for result in [civil_result, *piping_results.values()]:
        warnings.extend(result.get("warnings", []))
    warnings = list(dict.fromkeys(warnings))
    civil_result = deepcopy(civil_result)
    civil_result["network_id"] = network_id
    civil_result["density_kgm3"] = rho
    envelopes = _envelopes(network_id, ties, rows, solved_entries, applied, converged, tol_rel, warnings)
    return {"ok": True, "converged": converged, "iterations": len(history), "history": history,
            "tie_ins": rows, "civil_result": civil_result,
            "piping_result": next(iter(piping_results.values())) if len(piping_results) == 1 else piping_results,
            "simhandoff": envelopes, "warnings": warnings, "assumptions": assumptions, "method": dict(_METHOD)}


def civil_to_fea_handoff(result: dict, *, target_doc_id: str | None = None,
                         face_selector: str | None = None) -> dict:
    """Produce (not execute) static FEA loads with explicit N/Pa provenance.

    Thrust uses result.force_vector_n; a scalar thrust_kn never implies a global
    direction. Buried checks use result.pressure_kpa (e.g. prism/live pressure).
    Marston rigid/flexible results require the caller to select the applicable
    pressure_kpa; no structural load-model choice is silently made here.
    References: DIPRA Thrust Restraint Design for Ductile-Iron Pipe; hydrostatic
    pressure thrust and aicad.simhandoff/1 §4 load conventions.
    """
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise CivilError("invalid_input", "A successful civil thrust/buried result is required")
    if target_doc_id is not None:
        _text(target_doc_id, "target_doc_id")
    if face_selector is not None:
        _text(face_selector, "face_selector")
    conditions = []
    digest = {}
    if "force_vector_n" in result:
        vector = result["force_vector_n"]
        if not isinstance(vector, (list, tuple)) or len(vector) != 3:
            raise CivilError("invalid_input", "force_vector_n must be a finite three-component vector")
        vector = [_number(v, "force_vector_n") for v in vector]
        digest["force_vector_n"] = vector
        conditions.append(sh.condition("thrust_force", "force", vector, "N", "civil thrust result.force_vector_n, global Cartesian components in N", applied_to=face_selector))
    if "pressure_kpa" in result:
        pressure = _number(result["pressure_kpa"], "pressure_kpa")
        if pressure < 0:
            raise CivilError("invalid_input", "Buried crown pressure must be nonnegative")
        digest["pressure_kpa"] = pressure
        conditions.append(sh.condition("crown_pressure", "pressure", pressure * 1000.0, "Pa", "civil buried result.pressure_kpa multiplied by 1000; compressive pipe-crown pressure", applied_to=face_selector))
    if not conditions:
        reason = "force_direction_required" if "thrust_kn" in result else "load_selection_required"
        raise CivilError(reason, "Supply force_vector_n or the selected buried pressure_kpa; scalar thrust and unselected Marston alternatives are insufficient")
    warnings = list(result.get("warnings", []))
    if face_selector is None:
        warnings.append("Loads are unbound: select the target bearing/crown face before running FEA.")
    source_tool = "civil_thrust" if "force_vector_n" in digest else "civil_buried"
    envelope = sh.build_handoff(
        "civil_to_fea", sh.study_ref("civil", source_tool, result_digest=digest),
        sh.study_ref("fea", "run_fea_static", doc_id=target_doc_id),
        conditions=conditions, warnings=warnings,
        meta={"source_method": result.get("method", {}), "source_assumptions": result.get("assumptions", []),
              "face_selector": face_selector, "target_doc_id": target_doc_id})
    envelope.update({"ok": True, "warnings": warnings,
                     "assumptions": ["Quasi-static one-way load transfer; face selection and FEA setup remain the caller's responsibility."],
                     "method": {"name": "Civil static-load handoff", "reference": "DIPRA Thrust Restraint Design; aicad.simhandoff/1 §4; 1 kPa = 1000 Pa"}})
    return _validated(envelope)
