"""Civil buried/support checks and evidence handoffs persisted in ``civil_site`` studies.

Input geometry is civil SI metres, except native ``aicad.piping/1`` polylines
which remain in mm. Material and geotechnical design properties are caller inputs;
these screening calculations do not invent a code allowable or soil capacity.
"""
from __future__ import annotations

import copy
import math
from typing import Optional, Union

from services.civil import buried, piping_link, planning, schema, supports, terrain
from services.civil.errors import CivilError
from services.mcp import civil_common as cc, core_bridge as cb

_JSON = Union[str, dict, None]
_G = 9.80665


def _object(value, label):
    obj = cc.as_obj(value)
    if not isinstance(obj, dict):
        raise CivilError("bad_argument", f"{label} must be a JSON object")
    return obj


def _required(obj, key):
    if obj.get(key) is None:
        raise CivilError("missing_input", f"Supply {key}", field=key)
    return obj[key]


def _number(value, label, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)):
        raise CivilError("invalid_input", f"{label} must be a finite {'positive ' if positive else ''}number")
    return float(value)


def _saved(entry, key, result, target, next_actions):
    cc.store_result(entry, key, result)
    return {**result, **cc.persist(target, entry), "next_actions": next_actions}


def _combined(method, parts, **fields):
    return {"ok": True, **fields,
            "checks": [row for result in parts.values() for row in result.get("checks", [])],
            "warnings": list(dict.fromkeys(w for r in parts.values() for w in r.get("warnings", []))),
            "assumptions": list(dict.fromkeys(a for r in parts.values() for a in r.get("assumptions", []))),
            "method": {"name": method, "components": {k: v["method"] for k, v in parts.items()}},
            **parts}


def _conduit_cover(model, inputs, conduit_id, network_id, surface_id):
    if not all((conduit_id, network_id, surface_id)):
        raise CivilError("cover_references_required", "Supply conduit_id, network_id and surface_id together")
    net = schema.find(model, "gravity_networks", network_id)
    conduit = next((p for p in net["conduits"] if p["id"] == conduit_id), None)
    if conduit is None:
        raise CivilError("not_found", collection="conduits", id=conduit_id)
    if conduit.get("shape") != "circular":
        raise CivilError("unsupported_conduit_shape", "Buried ring checks require a circular conduit")
    nodes = {n["id"]: n for n in net["nodes"]}
    start, end = nodes[conduit["from"]], nodes[conduit["to"]]
    points = [[start["x"], start["y"]], *conduit.get("vertices", []), [end["x"], end["y"]]]
    lengths = [math.dist(a, b) for a, b in zip(points, points[1:])]
    total = sum(lengths)
    if total <= 0:
        raise CivilError("invalid_input", "Conduit plan length must be positive")
    fraction = _number(inputs.get("station_fraction", 0.5), "station_fraction")
    if not 0 <= fraction <= 1:
        raise CivilError("invalid_input", "station_fraction must be in [0,1]")
    remaining = fraction * total
    for a, b, length in zip(points, points[1:], lengths):
        if length > 0 and remaining <= length:
            xy = [x + (y-x)*remaining/length for x, y in zip(a, b)]
            break
        remaining -= length
    else:
        xy = points[-1]
    z1 = conduit.get("inlet_invert")
    z2 = conduit.get("outlet_invert")
    z1 = _number(start.get("invert") if z1 is None else z1, "inlet_invert")
    z2 = _number(end.get("invert") if z2 is None else z2, "outlet_invert")
    invert = z1 + fraction*(z2-z1)
    diameter = conduit["diameter"]
    ground = terrain.surface_sampler(schema.find(model, "surfaces", surface_id))(*xy)
    if ground is None:
        raise CivilError("outside_surface", "Conduit sample is outside the surface TIN", xy=xy)
    crown = invert + diameter
    inputs["cover_m"] = ground-crown
    inputs.setdefault("od_m", diameter)
    inputs.setdefault("diameter_m", diameter)
    return {"surface_id": surface_id, "network_id": network_id, "conduit_id": conduit_id,
            "station_fraction": fraction, "xy_m": xy, "ground_elevation_m": ground,
            "invert_elevation_m": invert, "crown_elevation_m": crown, "cover_m": ground-crown}


def _piping_doc(piping, network=None, target=None):
    # The resolver only reads the piping session: civil analysis must not change
    # the active plant network or its saved results.
    from services.mcp.piping_tools import _resolve_piping
    doc, name, refusal = _resolve_piping(piping, network)
    if refusal and target and refusal.get("reason") in {"no_piping", "unknown_network"}:
        wanted = network or (piping.strip() if isinstance(piping, str) else None)
        records = [s for s in cc.studies(target) if s.get("type") == "piping_network"
                   and isinstance((s.get("params") or {}).get("piping"), dict)]
        matches = [s for s in records if s.get("name") == wanted] if wanted else records
        if not matches:
            raise CivilError("network_not_found" if wanted and records else "no_piping_network",
                             "No matching saved piping network", networks=[s.get("name") for s in records])
        if len(matches) != 1:
            raise CivilError("network_ambiguous", "Select a saved piping network by name",
                             networks=[s.get("name") for s in matches])
        return matches[0]["params"]["piping"], matches[0].get("name")
    if refusal:
        raise CivilError(refusal["reason"], refusal.get("hint"),
                         **{k: v for k, v in refusal.items() if k not in {"ok", "reason", "hint"}})
    return doc, name


def _water_node(entry, network_id, node_id):
    result = entry.get("results", {}).get("water")
    if not isinstance(result, dict):
        raise CivilError("no_water_result", "Run civil_water_solve before deriving thrust pressure")
    network_id = network_id or result.get("network_id")
    if network_id is None:
        networks = entry["model"]["water_networks"]
        if len(networks) != 1:
            raise CivilError("network_id_required", "Select the water network")
        network_id = networks[0]["id"]
    if result.get("network_id", network_id) != network_id:
        raise CivilError("water_result_network_mismatch", "Stored water result belongs to another network")
    if result.get("converged") is not True:
        raise CivilError("civil_solver_failed", "Thrust pressure requires a converged water result")
    network = schema.find(entry["model"], "water_networks", network_id)
    node = next((n for n in network["junctions"] if n["id"] == node_id), None)
    if node is None:
        raise CivilError("not_found", collection="junctions", id=node_id)
    steps = result.get("timesteps") or []
    solved = (steps[0].get("nodes") or {}).get(node_id) if steps else None
    if not isinstance(solved, dict):
        raise CivilError("water_node_not_found", "Node is absent from the stored water result", node_id=node_id)
    rho = _number(result.get("density_kgm3", 1000.0), "density_kgm3", positive=True)
    head, elevation = _number(solved.get("head_m"), "head_m"), _number(node.get("elevation"), "elevation")
    return rho*_G*(head-elevation)/1000, network, {
        "network_id": network_id, "node_id": node_id, "timestep": 0,
        "head_m": head, "elevation_m": elevation, "density_kgm3": rho}


def _check_rows(result):
    if not isinstance(result, dict):
        return []
    if isinstance(result.get("checks"), list):
        return result["checks"]
    rows = []
    for key, value in result.items():
        if key == "simhandoff":
            continue  # Cross-solver agreement checks are not civil criterion rows.
        if isinstance(value, dict):
            rows.extend(_check_rows(value))
        elif isinstance(value, list):
            for item in value:
                rows.extend(_check_rows(item))
    return rows


def register_civil_link_tools(mcp) -> None:
    """Register civil structural screening and cross-domain evidence tools."""

    @mcp.tool()
    def civil_buried_pipe_check(inputs: Union[str, dict], conduit_id: Optional[str] = None,
                                network_id: Optional[str] = None, surface_id: Optional[str] = None,
                                site: _JSON = None, doc_id: Optional[str] = None) -> str:
        """Check buried pipe earth/live loads, Iowa deflection, buckling and flotation.

        UNITS: SI m, kPa, kN/m, kN/m3; inputs is an object with cover_m, od_m,
        gamma_kn_m3, wheel_load_kn, contact_w_m, contact_l_m, impact_factor,
        ring_stiffness_kpa, ring_ei_kn_m, e_prime_kpa (or soil_class/compaction),
        applied_pressure_kpa (net external), empty_weight_kn_m, gw_depth_m,
        gamma_dry_kn_m3, gamma_sat_kn_m3, fs_required (flotation).
        Optional diameter_m, trench_width_m/ku_prime/marston_load (flexible default),
        distribution/lldf, buckling_fs (2.5), hw_m (cover-gw depth, clipped at zero),
        and Iowa factors/method/material/criteria use the buried engine's names.
        With conduit/network/surface IDs, cover=surface z-(invert+diameter) at
        station_fraction of plan length (default 0.5); this is NOT a reach envelope.
        Returns component calculations, check rows and selected crown pressure;
        persists results.buried in civil_site when doc_id is supplied.
        Refuses missing_input, outside_surface, invalid_input and engine-named
        incompatible soil/load-basis cases; no implicit truck or soil allowable.
        next_actions: civil_fea_handoff, civil_requirement_evidence.
        """
        def work():
            target = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, target)
            data = copy.deepcopy(_object(inputs, "inputs"))
            cover_source = None
            if any((conduit_id, network_id, surface_id)):
                cover_source = _conduit_cover(entry["model"], data, conduit_id, network_id, surface_id)
            cover, od = _required(data, "cover_m"), _required(data, "od_m")
            gamma = _required(data, "gamma_kn_m3")
            basis = "marston" if data.get("trench_width_m") is not None else "prism"
            if basis == "marston":
                earth = buried.marston_trench_load(gamma, cover, data["trench_width_m"], od,
                                                   data.get("ku_prime", 0.13))
                selection = data.get("marston_load", "flexible")
                if selection not in {"flexible", "rigid"}:
                    raise CivilError("invalid_input", "marston_load must be flexible or rigid")
                earth_pressure = earth[f"{selection}_pressure_kpa"]
            else:
                earth = buried.prism_load(gamma, cover, od)
                earth_pressure = earth["pressure_kpa"]
                selection = "prism"
            live = buried.live_load_pressure(
                _required(data, "wheel_load_kn"), _required(data, "contact_w_m"),
                _required(data, "contact_l_m"), cover, _required(data, "impact_factor"),
                data.get("distribution", "aashto_lldf_1.15"), lldf=data.get("lldf", 1.15))
            iowa_keys = ("e_prime_kpa", "soil_class", "compaction", "saturated", "deflection_lag_factor",
                         "bedding_constant", "time_lag_factor", "design_factor", "material", "criteria")
            iowa_args = {k: data[k] for k in iowa_keys if k in data}
            if "criteria" in iowa_args:
                iowa_args["criteria"] = cc.as_obj(iowa_args["criteria"])
            else:
                criteria = entry["model"].get("criteria", {})
                if "allowable_deflection_pct_by_material" in criteria.get("overrides", criteria):
                    iowa_args["criteria"] = criteria
            diameter = data.get("diameter_m", od)
            deflection = buried.iowa_deflection(
                earth_pressure, live["pressure_kpa"], _required(data, "ring_stiffness_kpa"), diameter,
                method=data.get("method", "modified_iowa"), earth_load_basis=basis, **iowa_args)
            gw = _required(data, "gw_depth_m")
            hw = data.get("hw_m", max(0, cover-gw))
            buckling = buried.ring_buckling(diameter, _required(data, "ring_ei_kn_m"),
                                            deflection["e_prime_kpa"], cover, hw,
                                            _required(data, "applied_pressure_kpa"),
                                            fs=data.get("buckling_fs", 2.5))
            flotation = buried.flotation(od, _required(data, "empty_weight_kn_m"), cover, gw,
                                         _required(data, "gamma_dry_kn_m3"), _required(data, "gamma_sat_kn_m3"),
                                         _required(data, "fs_required"),
                                         gamma_water_kn_m3=data.get("gamma_water_kn_m3", _G))
            result = _combined("Buried pipe screening", {"earth": earth, "live": live,
                               "deflection": deflection, "buckling": buckling, "flotation": flotation},
                               inputs=data, cover_m=cover, cover_source=cover_source,
                               earth_load_basis=basis, selected_earth_load=selection,
                               pressure_kpa=earth_pressure+live["pressure_kpa"])
            result["assumptions"].append("FEA crown pressure is selected earth plus live pressure; buckling uses the caller's separate net external applied_pressure_kpa.")
            if cover_source:
                result["assumptions"].append("Conduit cover is a single plan-station sample, not a whole-reach envelope; model diameter locates the crown.")
            return _saved(entry, "buried", result, target,
                          ["civil_fea_handoff(source='buried')", "civil_requirement_evidence(source_key='buried', requirement_ids_by_check=...)"])
        return cc.run_tool(work)

    @mcp.tool()
    def civil_thrust_restraint(pressure_kpa: Optional[float] = None,
                               fitting: Union[str, dict] = "bend", angle_deg: Optional[float] = None,
                               diameter_m: Optional[float] = None, network_id: Optional[str] = None,
                               node_id: Optional[str] = None, bearing_kpa: Optional[float] = None,
                               restrained: _JSON = None, site: _JSON = None,
                               doc_id: Optional[str] = None) -> str:
        """Compute hydrostatic fitting thrust, optional bearing block and restrained legs.

        UNITS: m, kPa, kN; angle_deg is degrees; restrained friction_angle_rad is radians.
        Omitted pressure uses stored converged water head at node_id, timestep 0:
        p=rho*g*(head-elevation)/1000 kPa (freshwater rho=1000 unless stored).
        diameter_m is the pressure bore; omitted requires one incident pipe size.
        fitting is bend|tee|dead_end|reducer, or {kind, direction:[unit global XYZ],
        area2_m2/diameter2_m, block:{depth_m,height_max_m,fs,od_m}}. Direction
        is REQUIRED for a later thrust FEA handoff; scalar angles cannot imply it.
        bearing_kpa requires fitting.block geometry; no soil capacity is invented.
        restrained supplies DIPRA inputs: od_m, cover_m, gamma_kn_m3,
        pipe_weight_kn_m, contents_weight_kn_m, cohesion_kpa, friction_angle_rad,
        adhesion_factor, friction_angle_factor, bearing_reduction_factor, optional
        fs/polyethylene_encased; applies only to horizontal bends.
        Returns thrust_kn, optional force_vector_n/block/restraint; persists
        results.thrust in civil_site with doc_id. Refuses no_water_result,
        civil_solver_failed, diameter_required, block_geometry_required and engine reasons.
        next_actions: civil_fea_handoff (then run_fea_static).
        """
        def work():
            target = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, target)
            spec = {"kind": fitting} if isinstance(fitting, str) and not fitting.strip().startswith("{") else _object(fitting, "fitting")
            kind = spec.get("kind", "bend")
            pressure, network, source = pressure_kpa, None, None
            if pressure is None:
                pressure, network, source = _water_node(entry, network_id, node_id)
            elif network_id:
                network = schema.find(entry["model"], "water_networks", network_id)
            diameter = diameter_m
            if diameter is None and network is not None and node_id:
                diameters = {p["diameter"] for p in network["pipes"] if node_id in (p["from"], p["to"])}
                if len(diameters) == 1:
                    diameter = diameters.pop()
            if diameter is None:
                raise CivilError("diameter_required", "Supply the pressure bore diameter_m; incident pipe sizes are missing or ambiguous")
            area = math.pi*_number(diameter, "diameter_m", positive=True)**2/4
            angle = math.radians(_number(angle_deg, "angle_deg")) if angle_deg is not None else None
            a2 = spec.get("area2_m2")
            if spec.get("diameter2_m") is not None:
                if a2 is not None:
                    raise CivilError("bad_argument", "Supply area2_m2 or diameter2_m, not both")
                a2 = math.pi*_number(spec["diameter2_m"], "diameter2_m", positive=True)**2/4
            force = buried.thrust_force(pressure, area, kind, angle, a2, direction=spec.get("direction"))
            parts = {"force": force}
            if bearing_kpa is not None:
                block = spec.get("block")
                if not isinstance(block, dict) or block.get("depth_m") is None or block.get("height_max_m") is None:
                    raise CivilError("block_geometry_required", "Supply fitting.block.depth_m and height_max_m for bearing face sizing")
                parts["block"] = buried.thrust_block(force["thrust_kn"], bearing_kpa, block.get("fs", 1.5),
                                                     depth=block["depth_m"], height_max=block["height_max_m"], od_m=block.get("od_m"))
            if restrained is not None:
                if kind != "bend":
                    raise CivilError("unsupported_fitting", "Restrained length applies only to horizontal bends")
                parts["restraint"] = buried.restrained_length(pressure, area, angle, **_object(restrained, "restrained"))
            result = _combined("Hydrostatic thrust and restraint", parts,
                               thrust_kn=force["thrust_kn"], fitting=kind, design_pressure_kpa=pressure,
                               diameter_m=diameter, area_m2=area, pressure_source=source)
            if "force_vector_n" in force:
                result["force_vector_n"] = force["force_vector_n"]
            if source:
                result["assumptions"].append("Pressure derived from stored water timestep 0 with rho*g*(head-elevation); freshwater rho=1000 kg/m3 unless the result supplies density_kgm3. Surge/test pressure is not added.")
            return _saved(entry, "thrust", result, target, ["civil_fea_handoff(source='thrust', target_doc_id=..., face_selector=...)"])
        return cc.run_tool(work)

    @mcp.tool()
    def civil_pipe_supports(piping: _JSON = None, network: Optional[str] = None,
                             material: _JSON = None, elastic_modulus_gpa: Optional[float] = None,
                             allowable_stress_mpa: Optional[float] = None,
                             pipe_density_kg_m3: Optional[float] = None,
                             fluid_density_kg_m3: float = 1000.0, insulation_kg_m: float = 0.0,
                             allowable_sag_mm: float = 2.5, condition: str = "simply_supported",
                             run_properties: _JSON = None, fitting_weights_n: _JSON = None,
                             fitting_tolerance_mm: float = 1.0, friction_coefficient: Optional[float] = None,
                             beam_assignments: _JSON = None, site: _JSON = None,
                             doc_id: Optional[str] = None) -> str:
        """Plan pipe supports at bends, branches and fittings and fill span-limited gaps.

        UNITS: native piping paths mm, result positions m/mm, reactions N;
        material properties GPa, MPa, kg/m3; allowable_sag_mm is mm.
        piping accepts inline aicad.piping/1, JSON/path, session name or None.
        network selects a session network, else a saved piping_network in doc_id;
        resolving it does not change the active piping session.
        material is {elastic_modulus_gpa,allowable_stress_mpa,pipe_density_kg_m3},
        or supply those keyword arguments; no allowable is inferred from a name.
        allowable_stress_mpa is AVAILABLE bending budget at operating temperature.
        Optional run_properties overrides these by run; fitting_weights_n maps ID
        to N. friction_coefficient optionally adds rack_loads by beam_assignments.
        Returns supports, spacing and total weight/reactions; persists
        results.supports in civil_site with doc_id. Refuses missing_input,
        no_piping_network/network_ambiguous, unknown sizes and engine reasons.
        next_actions: run_fea_static for support/rack structural verification.
        """
        def work():
            target = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, target)
            doc, name = _piping_doc(piping, network, target)
            props = {} if material is None else _object(material, "material")
            for key, value in (("elastic_modulus_gpa", elastic_modulus_gpa),
                               ("allowable_stress_mpa", allowable_stress_mpa),
                               ("pipe_density_kg_m3", pipe_density_kg_m3)):
                if value is not None:
                    props[key] = value
            result = supports.supports_for_piping(
                doc, elastic_modulus_gpa=_required(props, "elastic_modulus_gpa"),
                allowable_stress_mpa=_required(props, "allowable_stress_mpa"),
                pipe_density_kg_m3=_required(props, "pipe_density_kg_m3"),
                fluid_density_kg_m3=fluid_density_kg_m3, insulation_kg_m=insulation_kg_m,
                allowable_sag_mm=allowable_sag_mm, condition=condition,
                run_properties=cc.as_obj(run_properties), fitting_weights_n=cc.as_obj(fitting_weights_n),
                fitting_tolerance_mm=fitting_tolerance_mm)
            result["network"] = name
            if friction_coefficient is not None:
                rack = supports.rack_loads(result["supports"], friction_coefficient=friction_coefficient,
                                           beam_assignments=cc.as_obj(beam_assignments))
                result["rack"] = rack
                result["warnings"] += rack.get("warnings", [])
            return _saved(entry, "supports", result, target, ["run_fea_static(...) for support/rack loads; tributary reactions are not a structural code acceptance"])
        return cc.run_tool(work)

    @mcp.tool()
    def civil_piping_couple(network_id: str, piping: Union[str, dict],
                            boundary: Union[str, dict, list, None] = None, fluid: _JSON = None,
                            tie_in_ids: Union[str, list, None] = None, tol_rel: float = 0.01,
                            max_iter: int = 30, site: _JSON = None,
                            doc_id: Optional[str] = None) -> str:
        """Couple civil water supply with a real native plant piping solve to convergence.

        UNITS: civil m/m3/s, plant geometry mm, native pressure kPa and flow m3/h.
        piping is inline/session piping, {doc,boundary,fluid}, or a piping_doc_id
        keyed map of those inputs for multiple plants; optional tie_in_ids selects
        model interfaces. boundary is native run/end conditions; fluid supplies
        rho kg/m3 and mu Pa*s (omitted means explicitly disclosed freshwater).
        Calls core piping_solve each iteration, never a synthetic plant fallback.
        Requires steady DDA civil network and incompressible liquid plants.
        Returns converged flag, head/flow agreement, iteration history and validated
        aicad.simhandoff/1 envelopes; persists results.tie_ins in civil_site.
        Refuses piping_solver_failed (with solver_reason), missing/ambiguous tie-ins,
        conflicting_tie_in_boundary and unsupported coupling modes. Nonconvergence
        is reported honestly with warnings and failed agreement, not a success claim.
        next_actions: civil_requirement_evidence, piping_report.
        """
        def work():
            target = cc.target_doc(doc_id)
            entry = cc.resolve_site(site, target)
            value = piping
            if isinstance(value, str) and (value.strip().startswith("{") or value.strip().endswith(".json")):
                value = cc.as_obj(value)
            if isinstance(value, dict) and value.get("schema") is None and "piping" not in value:
                inputs = copy.deepcopy(value)
                if "doc" not in inputs and any(v is not None for v in (boundary, fluid)):
                    raise CivilError("bad_argument", "For multiple plants supply boundary/fluid inside each piping_doc_id entry")
                if "doc" in inputs:
                    if boundary is not None:
                        inputs["boundary"] = cc.as_obj(boundary)
                    if fluid is not None:
                        inputs["fluid"] = cc.as_obj(fluid)
            else:
                doc, _ = _piping_doc(value, target=target)
                inputs = {"doc": doc, "boundary": cc.as_obj(boundary) if boundary is not None else [],
                          "fluid": cc.as_obj(fluid)}
            if tie_in_ids is not None:
                inputs["tie_in_ids"] = cc.as_obj(tie_in_ids)

            def solver(doc, bc, resolved_fluid):
                return cb.call("piping_solve", {"doc": doc, "boundary": bc, "fluid": resolved_fluid})

            result = piping_link.couple(entry["model"], network_id, solver, piping_inputs=inputs,
                                        tol_rel=tol_rel, max_iter=max_iter)
            return _saved(entry, "tie_ins", result, target,
                          ["civil_requirement_evidence(source_key='tie_ins', requirement_ids_by_check=...)", "piping_report(...) after a standalone piping_solve if a plant report is needed"])
        return cc.run_tool(work)

    @mcp.tool()
    def civil_fea_handoff(source: str = "thrust", target_doc_id: Optional[str] = None,
                           face_selector: Optional[str] = None, site: _JSON = None) -> str:
        """Translate stored thrust/buried loads into a validated static FEA handoff.

        UNITS: stored kN/kPa translate to global force N/crown pressure Pa.
        Does not execute FEA or persist a second study; source remains in civil_site.
        Requires a stored successful source; thrust additionally requires an explicit
        global fitting.direction from civil_thrust_restraint. Face selection binds
        the load; omitted selector is honestly reported as an unbound-load warning.
        Returns an aicad.simhandoff/1 civil_to_fea envelope. Refuses no_thrust_result,
        no_buried_result, force_direction_required or load_selection_required.
        next_actions: run_fea_static with the selected face and transferred loads.
        """
        def work():
            if source not in {"thrust", "buried"}:
                raise CivilError("unsupported_source", "source must be thrust or buried")
            entry = cc.resolve_site(site)
            result = entry.get("results", {}).get(source)
            if not isinstance(result, dict):
                raise CivilError(f"no_{source}_result", f"Run civil_{'thrust_restraint' if source == 'thrust' else 'buried_pipe_check'} first")
            envelope = piping_link.civil_to_fea_handoff({**result, "ok": True},
                                                       target_doc_id=target_doc_id, face_selector=face_selector)
            return {**envelope, "next_actions": ["run_fea_static(...): apply the handoff conditions to face_selector; FEA has not been executed"]}
        return cc.run_tool(work)

    @mcp.tool()
    def civil_requirement_evidence(requirement_ids_by_check: Union[str, dict],
                                    source_key: str = "gravity", site: _JSON = None) -> str:
        """Map stored civil check rows to requirement evidence without inventing a pass.

        UNITS: each row retains its original value/limit/units/reference unchanged.
        requirement_ids_by_check maps check name to one requirement ID or an ID list.
        source_key selects an existing stored result (water, gravity, buried, etc.).
        No requirement or civil study is edited; returns evidence_by_requirement and
        unmapped rows for explicit req_update use. Failing checks remain failing.
        Refuses no_<source_key>_result, no_check_rows or invalid_check_row; a coupling
        agreement envelope is not a civil criterion row and is not silently recast.
        next_actions: req_update with the returned evidence for each requirement.
        """
        def work():
            if source_key not in cc.RESULT_KEYS:
                raise CivilError("unknown_result_key", f"Unknown civil source {source_key!r}")
            entry = cc.resolve_site(site)
            result = entry.get("results", {}).get(source_key)
            if not isinstance(result, dict):
                raise CivilError(f"no_{source_key}_result", f"Run the {source_key} analysis first")
            rows = _check_rows(result)
            if not rows:
                raise CivilError("no_check_rows", "Stored analysis has no civil criterion check rows", source_key=source_key)
            result = planning.requirement_evidence(rows, _object(requirement_ids_by_check, "requirement_ids_by_check"))
            return {**result, "source_key": source_key,
                    "next_actions": ["req_update(..., evidence=...) for each evidence_by_requirement entry; do not infer requirement approval from a single check"]}
        return cc.run_tool(work)
