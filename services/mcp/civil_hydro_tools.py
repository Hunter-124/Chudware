"""Agent-facing SI civil hydraulics, hydrology and detention tools.

Analyses keep their full engineering tables in the shared civil-site session and,
when requested, its document study. Only the returned water/gravity view is compact;
``detail=True`` returns the complete tables. Engine modules own all equations and
jurisdictional criteria resolution.
"""
from __future__ import annotations

from typing import Any, Optional, Union

from services.civil import culvert as culvert_engine
from services.civil import criteria, gravity, hydrology, schema, terrain, water
from services.civil.errors import CivilError
from services.mcp import civil_common as cc

Site = Union[str, dict, None]
_HEAD_TO_KPA = 9.80665  # rho=1000 kg/m3, g=9.80665 m/s2


def _context(site: Site, doc_id: Optional[str]) -> tuple[dict, Optional[str]]:
    doc = cc.target_doc(doc_id)
    return cc.resolve_site(site, doc), doc


def _finish(entry: dict, doc: Optional[str], key: str, result: dict,
            next_actions: list[str]) -> dict:
    cc.store_result(entry, key, result)
    return {**result, **cc.persist(doc, entry), "next_actions": next_actions}


def _checks_first(result: dict) -> None:
    """Partition failures before passes; order within either group is not a severity rank."""
    result["checks"] = sorted(result.get("checks", []), key=lambda row: bool(row["pass"]))


def _extrema(pressures: list[tuple[str, float, Optional[float]]],
             velocities: list[tuple[str, float, Optional[float]]], checks: list[dict]) -> dict:
    summary = {"checks_failed": sum(not row["pass"] for row in checks),
               "checks_total": len(checks)}
    for label, picker in (("min", min), ("max", max)):
        node, value, t = picker(pressures, key=lambda row: row[1]) if pressures else (None, None, None)
        summary.update({f"{label}_pressure_m": value, f"{label}_pressure_kpa":
                        value * _HEAD_TO_KPA if value is not None else None,
                        f"{label}_pressure_node_id": node, f"{label}_pressure_t_s": t})
    element, velocity, t = max(velocities, key=lambda row: row[1]) if velocities else (None, None, None)
    summary.update(max_velocity_ms=velocity, max_velocity_element_id=element, max_velocity_t_s=t)
    return summary


def _water_summary(result: dict, network: dict) -> dict:
    junctions = {node["id"] for node in network["junctions"]}
    pressures = [(nid, node["pressure_m"], step["t_s"])
                 for step in result["timesteps"] for nid, node in step["nodes"].items()
                 if nid in junctions]
    velocities = [(lid, link["velocity_ms"], step["t_s"])
                  for step in result["timesteps"] for lid, link in step["links"].items()]
    return {**_extrema(pressures, velocities, result["checks"]),
            "hydraulic_instants": len(result["timesteps"]),
            "pressure_basis": "Junction gauge pressure; fixed-head reservoirs are excluded."}


def _gravity_summary(result: dict, network: dict) -> dict:
    inverts = {node["id"]: node["invert"] for node in network["nodes"]}
    pressures = [(nid, node["hgl_m"] - inverts[nid], None)
                 for nid, node in result["nodes"].items()]
    velocities = [(pid, row.get("hydraulic_velocity_ms", row["velocity_ms"]), None)
                  for pid, row in result["conduits"].items()]
    return {**_extrema(pressures, velocities, result["checks"]),
            "total_inflow_m3s": result["total_inflow_m3s"],
            "total_outflow_m3s": result["total_outflow_m3s"],
            "surcharged_node_ids": [nid for nid, row in result["nodes"].items() if row["surcharged"]],
            "flooding_node_ids": [nid for nid, row in result["nodes"].items() if row["flooding"]],
            "pressure_basis": "HGL minus node invert (water-column head), not gauge pressure at the pipe crown."}


def _tc(catchment: dict) -> dict:
    if catchment.get("tc_s") is not None:
        return {"tc_s": catchment["tc_s"], "warnings": [], "assumptions": []}
    return hydrology.time_of_concentration(catchment.get("tc_segments"))


def _rational(catchment: dict, storm: dict, *, c=None, area_m2=None, tc_s=None) -> dict:
    if not isinstance(storm, dict):
        raise CivilError("storm_required", "Supply intensity_mm_h or an IDF table and return_period_yr.")
    concentration = {"warnings": [], "assumptions": []}
    intensity = storm.get("intensity_mm_h")
    idf_result = None
    if intensity is None:
        idf = storm.get("idf_table", storm.get("idf"))
        if idf is None and "durations_s" in storm:
            idf = storm
        if not isinstance(idf, dict):
            raise CivilError("idf_required", "Supply intensity_mm_h or idf_table with return_period_yr.")
        period = storm.get("return_period_yr", idf.get("return_period_yr"))
        concentration = _tc(catchment) if tc_s is None else {"tc_s": tc_s, "warnings": [], "assumptions": []}
        idf_result = hydrology.idf_intensity(idf.get("table", idf), concentration["tc_s"], period)
        intensity = idf_result["intensity_mm_h"]
    result = hydrology.rational_peak(catchment.get("runoff_coeff") if c is None else c,
                                    intensity, catchment.get("area_m2") if area_m2 is None else area_m2)
    result.update(intensity_mm_h=intensity)
    if "tc_s" in concentration:
        result["tc_s"] = concentration["tc_s"]
    elif tc_s is not None or catchment.get("tc_s") is not None:
        result["tc_s"] = tc_s if tc_s is not None else catchment["tc_s"]
    for source in (concentration, idf_result or {}):
        for key in ("warnings", "assumptions"):
            result[key].extend(source.get(key, []))
    if idf_result:
        result["idf"] = idf_result
    return result


def _storm(storm: Any, dt_s: float) -> dict:
    spec = cc.as_obj(storm)
    if not isinstance(spec, dict):
        raise CivilError("storm_required", "Supply t_s/depth_mm hyetograph or a design-storm specification.")
    if "t_s" in spec:
        return spec
    idf = spec.get("idf")
    if idf is None and spec.get("idf_table") is not None:
        table = spec["idf_table"]
        period = spec.get("return_period_yr")
        if period is None and isinstance(table, dict):
            period = table.get("return_period_yr")
        idf = {"table": table, "return_period_yr": period}
    return hydrology.design_storm(spec.get("kind", "scs_type_ii"), spec.get("depth_mm"),
                                  spec.get("duration_s", 86400), spec.get("dt_s", dt_s),
                                  idf)


def register_civil_hydro_tools(mcp) -> None:
    @mcp.tool()
    def civil_water_solve(network_id: str, site: Site = None, doc_id: Optional[str] = None,
                          detail: bool = False) -> str:
        """Solve a site's pressurized water network and screen its jurisdictional limits.

        UNITS: SI m, m3/s, m/s; pressure head m and summary gauge pressure kPa.
        Model criteria are resolved by the engine; full water results persist in civil_site.
        Returns convergence/residual, pressure extrema with node ids, maximum velocity,
        and failing checks first. detail=True includes every hydraulic-instant node/link table.
        Refuses missing site/network, disconnected nodes, unsupported hydraulics or bad inputs;
        a non-converged solve is explicitly reported, never called converged.
        next_actions: revise failed checks, civil_fire_flow, civil_load.
        """
        def run():
            entry, doc = _context(site, doc_id)
            network = schema.find(entry["model"], "water_networks", network_id)
            result = water.solve(network, criteria=entry["model"]["criteria"])
            _checks_first(result)
            result.update(network_id=network_id, summary=_water_summary(result, network))
            out = _finish(entry, doc, "water", result, ["civil_fire_flow", "civil_site_edit", "civil_load"])
            if not detail:
                out.pop("timesteps", None)
            return out
        return cc.run_tool(run)

    @mcp.tool()
    def civil_fire_flow(network_id: str, node_id: str, flow_m3s: Optional[float] = None,
                        residual_min_m: Optional[float] = None, site: Site = None,
                        doc_id: Optional[str] = None, detail: bool = False) -> str:
        """Test fixed fire withdrawal or find the system-wide residual-pressure flow limit.

        UNITS: SI flow m3/s, residual head m; summary pressure kPa, velocity m/s.
        Omitting flow_m3s bisects to the limiting junction's residual boundary, using site criteria.
        Persists full fire_flow results in civil_site; baseline demands are not edited.
        Returns available flow, limiting node, residual pressures, pass and compact hydraulics;
        detail=True includes all hydraulic node/link tables; failed checks precede passes,
        without severity ranking within either group.
        Refuses non-junction targets, invalid withdrawal/residual, nonconvergence or no finite limit.
        Initial-state scenario only, not fire-duration supply/storage endurance.
        next_actions: civil_water_solve, civil_site_edit, civil_load.
        """
        def run():
            entry, doc = _context(site, doc_id)
            network = schema.find(entry["model"], "water_networks", network_id)
            result = water.fire_flow(network, node_id, flow_m3s, residual_min_m=residual_min_m,
                                     criteria=entry["model"]["criteria"])
            hydraulics = result["hydraulics"]
            limits = criteria.resolve(entry["model"]["criteria"])
            reference = limits["references"]["fire_residual_min_m"]
            if residual_min_m is not None:
                reference = f"user-supplied residual_min_m; profile reference: {reference}"
            hydraulics["checks"].extend(
                {"check": "fire_residual_pressure", "element_id": nid, "t_s": 0,
                 "value": pressure, "limit": result["residual_min_m"], "units": "m",
                 "pass": pressure >= result["residual_min_m"] - 1e-7,
                 "criterion": "fire_residual_min_m", "reference": reference}
                for nid, pressure in result["residual_pressures_m"].items())
            _checks_first(hydraulics)
            result.update(network_id=network_id, summary=_water_summary(hydraulics, network))
            result["checks"] = hydraulics["checks"]
            out = _finish(entry, doc, "fire_flow", result, ["civil_water_solve", "civil_site_edit", "civil_load"])
            if not detail:
                out["hydraulics"] = {k: v for k, v in hydraulics.items() if k != "timesteps"}
            return out
        return cc.run_tool(run)

    @mcp.tool()
    def civil_gravity_analyze(network_id: str, flows: Any = None, catchment_peaks: bool = False,
                              storm: Any = None, surface_id: Optional[str] = None,
                              site: Site = None, doc_id: Optional[str] = None,
                              detail: bool = False) -> str:
        """Accumulate gravity inflows, screen HGL/EGL and apply site design-criteria checks.

        UNITS: SI m, m3/s, m/s; slope m/m; rainfall intensity mm/h; summary head m/kPa.
        flows maps node/conduit ids to added m3/s. catchment_peaks adds C*i*A at each matching
        catchment outlet; storm supplies intensity_mm_h or idf_table+return_period_yr (Tc used).
        surface_id samples ground for cover, otherwise endpoint rims are used when available.
        Persists full gravity results in civil_site. Returns extrema, continuity, failing checks
        first and catchment peaks; detail=True adds all node/conduit HGL/EGL and flow tables.
        Gravity summary pressure is HGL minus invert, not pipe-crown gauge pressure.
        Refuses cycles, split routing, adverse slope, missing runoff/IDF inputs or terrain gaps.
        Peaks are simultaneous, with no storage/attenuation; HGL screening is preliminary.
        next_actions: civil_gravity_design, civil_hydrology, civil_site_edit.
        """
        def run():
            entry, doc = _context(site, doc_id)
            model = entry["model"]
            network = schema.find(model, "gravity_networks", network_id)
            added = cc.as_obj(flows)
            if added is not None and not isinstance(added, dict):
                raise CivilError("invalid_flows")
            added = dict(added or {})
            peaks = {}
            if catchment_peaks:
                event = cc.as_obj(storm)
                node_ids = {node["id"] for node in network["nodes"]}
                for catchment in model["catchments"]:
                    if catchment["outlet"] in node_ids:
                        peak = _rational(catchment, event)
                        peaks[catchment["id"]] = {**peak, "outlet_node_id": catchment["outlet"]}
                        outlet = catchment["outlet"]
                        added[outlet] = added.get(outlet, 0) + peak["flow_m3s"]
            z_at = terrain.surface_sampler(schema.find(model, "surfaces", surface_id)) if surface_id else None
            # analyze invokes gravity.check itself, including profile/terrain-cover rows.
            result = gravity.analyze(network, flows=added, z_at=z_at, criteria=model["criteria"])
            _checks_first(result)
            result.update(network_id=network_id, summary=_gravity_summary(result, network))
            if catchment_peaks:
                result["catchment_peaks"] = peaks
                for key in ("warnings", "assumptions"):
                    result[key] = list(dict.fromkeys(result[key] + [item for peak in peaks.values() for item in peak[key]]))
            out = _finish(entry, doc, "gravity", result, ["civil_gravity_design", "civil_hydrology", "civil_site_edit"])
            if not detail:
                out.pop("nodes", None)
                out.pop("conduits", None)
            return out
        return cc.run_tool(run)

    @mcp.tool()
    def civil_gravity_design(network_id: str, flows: Any = None, available_diameters: Any = None,
                             apply: bool = False, site: Site = None,
                             doc_id: Optional[str] = None) -> str:
        """Choose the smallest feasible supplied circular conduit sizes under site criteria.

        UNITS: diameters m, flow m3/s, velocity m/s. Supply available_diameters explicitly.
        flows maps node/conduit ids to added inflows. apply=False leaves the model unchanged;
        apply=True writes selected diameters and drops stale results through civil_site editing.
        Persists the new gravity design result; returns chosen sizes and old/new diameter changes.
        Refuses unsupported shapes, adverse slopes, invalid diameter list or no feasible size.
        Diameter sizing is a preliminary reach screen; rerun full gravity/cover/crown checks.
        next_actions: civil_gravity_analyze, civil_site_summary, civil_load.
        """
        def run():
            entry, doc = _context(site, doc_id)
            model = entry["model"]
            network = schema.find(model, "gravity_networks", network_id)
            result = gravity.design_sizes(network, cc.as_obj(flows), model["criteria"],
                                          cc.as_obj(available_diameters))
            old = {pipe["id"]: pipe["diameter"] for pipe in network["conduits"]}
            changes = [{"conduit_id": pid, "old_diameter_m": old[pid], "new_diameter_m": row["diameter_m"]}
                       for pid, row in result["sizes"].items() if old[pid] != row["diameter_m"]]
            if apply:
                updated = cc.model_copy(entry)
                schema.upsert(updated, "gravity_networks", result["network"])
                cc.set_model(entry, updated)
            result.update(network_id=network_id, applied=apply, diameter_changes=changes)
            return _finish(entry, doc, "gravity", result, ["civil_gravity_analyze", "civil_site_summary", "civil_load"])
        return cc.run_tool(run)

    @mcp.tool()
    def civil_open_channel(kind: str, shape: Any, flow_m3s: float, n: Optional[float] = None,
                           slope: Optional[float] = None, depth_m: Optional[float] = None,
                           length_m: Optional[float] = None, control_depth_m: Optional[float] = None,
                           direction: str = "upstream", steps: int = 100,
                           y1_m: Optional[float] = None) -> str:
        """Compute normal/critical depth, standard-step profile, jump or channel capacity.

        UNITS: SI m, m3/s, m/s, slope m/m; Manning n. Stateless: nothing is persisted.
        shape={shape: circular|rectangular|trapezoidal, diameter|width|height, side_slope}.
        kind=normal_depth requires n,slope; critical_depth uses only shape/flow;
        standard_step adds length_m,control_depth_m,direction,steps; hydraulic_jump uses y1_m.
        capacity uses n,slope and depth_m, or full/bankfull section height when depth_m omitted;
        flow_m3s is unused for capacity. Returns engineering depths/flow/profile and method.
        Refuses unsupported kind/shape, missing inputs, exceeded section/capacity, subcritical
        jump or an unstable/critical-crossing standard-step profile; no hidden clipping.
        next_actions: civil_gravity_analyze, civil_detention_route.
        """
        def run():
            section = cc.as_obj(shape)
            if kind == "normal_depth":
                result = gravity.normal_depth(section, flow_m3s, slope, n)
            elif kind == "critical_depth":
                result = gravity.critical_depth(section, flow_m3s)
            elif kind == "standard_step":
                result = gravity.standard_step(section, flow_m3s, n, slope, length_m,
                                               control_depth_m, direction, steps)
            elif kind == "hydraulic_jump":
                result = gravity.hydraulic_jump(section, flow_m3s, y1_m)
            elif kind == "capacity":
                result = (gravity.full_flow_capacity(section, slope, n) if depth_m is None else
                          gravity.manning_flow(section, depth_m, slope, n))
            else:
                raise CivilError("unsupported_open_channel_kind", kind=kind)
            return {**result, "kind": kind, "next_actions": ["civil_gravity_analyze", "civil_detention_route"]}
        return cc.run_tool(run)

    @mcp.tool()
    def civil_hydrology(kind: str, catchment_id: Optional[str] = None,
                        c: Optional[float] = None, intensity_mm_h: Optional[float] = None,
                        area_m2: Optional[float] = None, p_mm: Optional[float] = None,
                        cn: Optional[float] = None, ia_ratio: Optional[float] = None, segments: Any = None,
                        tc_s: Optional[float] = None, dt_s: float = 300,
                        peak_rate_factor: Optional[float] = None, storm: Any = None,
                        storm_kind: str = "scs_type_ii", depth_mm: Optional[float] = None,
                        duration_s: float = 86400, idf_table: Any = None,
                        return_period_yr: Optional[float] = None, idf_csv: Optional[str] = None,
                        site: Site = None, doc_id: Optional[str] = None) -> str:
        """Compute rational/CN runoff, Tc, unit hydrograph, storm, runoff hydrograph or IDF.

        UNITS: area m2, flow m3/s, time s, rainfall depth mm/intensity mm/h; C,CN dimensionless.
        kind=rational uses c,intensity_mm_h,area_m2 (or catchment fields); IDF uses Tc and
        idf_table+return_period_yr. scs_runoff uses p_mm,cn,ia_ratio; tc uses segments or
        catchment tc_segments; unit_hydrograph uses area_m2,tc_s,dt_s,peak_rate_factor.
        design_storm uses storm_kind,depth_mm,duration_s,dt_s,idf_table+return_period_yr.
        runoff_hydrograph uses catchment_id and storm={t_s,depth_mm interval depths} or storm
        specification; dt_s must match rainfall intervals. idf parses idf_csv or interpolates
        idf_table at duration_s/return_period_yr. Catchment fields supply omitted area/C/CN/Tc.
        Persists hydrology results in civil_site; does not edit catchment model parameters.
        Returns computed engineering values, hydrographs, warnings, assumptions and method.
        Refuses missing inputs, unknown kind, IDF extrapolation, unsupported storms or time grids.
        next_actions: civil_gravity_analyze, civil_detention_route, civil_load.
        """
        def run():
            entry, doc = _context(site, doc_id)
            catchment = schema.find(entry["model"], "catchments", catchment_id) if catchment_id else {}
            area = catchment.get("area_m2") if area_m2 is None else area_m2
            curve_number = catchment.get("cn") if cn is None else cn
            abstraction_ratio = catchment.get("ia_ratio", 0.2) if ia_ratio is None else ia_ratio
            prf = catchment.get("peak_rate_factor", 484) if peak_rate_factor is None else peak_rate_factor
            table = cc.as_obj(idf_table)
            event = cc.as_obj(storm)
            if kind == "rational":
                event = dict(event or {})
                if intensity_mm_h is not None:
                    event["intensity_mm_h"] = intensity_mm_h
                if table is not None:
                    event["idf_table"] = table
                if return_period_yr is not None:
                    event["return_period_yr"] = return_period_yr
                result = _rational(catchment, event, c=c, area_m2=area, tc_s=tc_s)
            elif kind == "scs_runoff":
                result = hydrology.scs_runoff_depth(p_mm, curve_number, abstraction_ratio)
            elif kind == "tc":
                result = hydrology.time_of_concentration(cc.as_obj(segments) if segments is not None else catchment.get("tc_segments"))
            elif kind == "unit_hydrograph":
                concentration = _tc(catchment) if tc_s is None else {"tc_s": tc_s, "warnings": [], "assumptions": []}
                result = hydrology.nrcs_unit_hydrograph(area, concentration["tc_s"], dt_s, prf)
                for key in ("warnings", "assumptions"):
                    result[key].extend(concentration[key])
            elif kind == "design_storm":
                idf = {"table": table, "return_period_yr": return_period_yr} if table is not None else None
                result = hydrology.design_storm(storm_kind, depth_mm, duration_s, dt_s, idf)
            elif kind == "runoff_hydrograph":
                inputs = {**catchment, "area_m2": area, "cn": curve_number, "ia_ratio": abstraction_ratio,
                          "peak_rate_factor": prf}
                if tc_s is not None:
                    inputs["tc_s"] = tc_s
                if segments is not None:
                    inputs["tc_segments"] = cc.as_obj(segments)
                result = hydrology.runoff_hydrograph(inputs, _storm(event, dt_s), dt_s)
            elif kind == "idf":
                result = (hydrology.idf_from_csv(idf_csv) if idf_csv is not None else
                          hydrology.idf_intensity(table, duration_s, return_period_yr))
            else:
                raise CivilError("unsupported_hydrology_kind", kind=kind)
            result.update(kind=kind, catchment_id=catchment_id)
            return _finish(entry, doc, "hydrology", result, ["civil_gravity_analyze", "civil_detention_route", "civil_load"])
        return cc.run_tool(run)

    @mcp.tool()
    def civil_detention_route(structure_id: Optional[str] = None, inflow: Any = None,
                              catchment_id: Optional[str] = None, storm: Any = None,
                              stage_storage: Any = None, surface_id: Optional[str] = None,
                              polygon: Any = None, stages: Any = None, outlets: Any = None,
                              initial_stage: Optional[float] = None, dt_s: float = 60,
                              site: Site = None, doc_id: Optional[str] = None) -> str:
        """Route a real inflow through level-pool detention with Modified Puls continuity.

        UNITS: stages/elevations m, storage m3, inflow/outflow m3/s, time s.
        Supply inflow={t_s,q_m3s}, or catchment_id+storm for a CN runoff hydrograph.
        stage_storage=[[stage_m,volume_m3],...] or surface_id+polygon+stages computes the
        exact terrain pond volume. outlets are explicit orifice/weir/riser/rating_curve inputs.
        initial_stage defaults to the table's lowest stage, not an invented initial volume.
        Existing structure_id supplies omitted storage/outlets/initial stage; when given, writes
        a detention_pond structure and drops obsolete analyses if its model changes.
        Persists full detention result in civil_site, including mass balance and ending storage.
        Returns stage/storage/outflow histories, peak attenuation and continuity error.
        Refuses missing inflow/storage, nonmonotonic ratings, terrain gaps, unsupported/submerged
        outlets, storage overflow or too-large timestep; no hidden spillway or extrapolation.
        next_actions: revise outlet/stage data, civil_hydrology, civil_load.
        """
        def run():
            entry, doc = _context(site, doc_id)
            model = entry["model"]
            existing = next((item for item in model["structures"] if item["id"] == structure_id), {}) if structure_id else {}
            if existing and existing["kind"] != "detention_pond":
                raise CivilError("structure_kind_mismatch", structure_id=structure_id, expected="detention_pond")
            catchment = schema.find(model, "catchments", catchment_id) if catchment_id else None
            runoff = None
            if inflow is not None:
                hydrograph = cc.as_obj(inflow)
            elif catchment is not None:
                event = cc.as_obj(storm)
                runoff_dt = event.get("dt_s", dt_s) if isinstance(event, dict) else dt_s
                runoff = hydrology.runoff_hydrograph(catchment, _storm(event, runoff_dt), runoff_dt)
                hydrograph = runoff
            else:
                raise CivilError("inflow_required", "Supply inflow={t_s,q_m3s} or catchment_id+storm.")
            footprint = cc.as_obj(polygon) if polygon is not None else existing.get("polygon")
            levels = cc.as_obj(stages) if stages is not None else existing.get("stages")
            storage = cc.as_obj(stage_storage)
            terrain_result = None
            if storage is None and surface_id:
                if footprint is None or levels is None:
                    raise CivilError("pond_geometry_required", "Terrain storage requires polygon and stages.")
                terrain_result = terrain.stage_storage(schema.find(model, "surfaces", surface_id), footprint, levels)
                storage = terrain_result["stage_storage"]
            elif storage is None:
                storage = existing.get("stage_storage")
            if isinstance(storage, dict):
                storage = storage.get("stage_storage")
            if storage is None:
                raise CivilError("stage_storage_required", "Supply stage_storage or surface_id+polygon+stages.")
            outlet_specs = cc.as_obj(outlets) if outlets is not None else existing.get("outlets")
            initial = initial_stage if initial_stage is not None else existing.get("initial_stage")
            if initial is None:
                if not isinstance(storage, list) or not storage or not isinstance(storage[0], (list, tuple)) or len(storage[0]) != 2:
                    raise CivilError("invalid_rating_curve", field="stage_storage")
                initial = storage[0][0]
            result = hydrology.modified_puls(hydrograph, storage, outlet_specs, initial, dt_s)
            result.update(structure_id=structure_id, catchment_id=catchment_id, stage_storage=storage)
            if runoff is not None:
                result["runoff_hydrograph"] = runoff
            if terrain_result is not None:
                result["terrain_storage"] = terrain_result
            for source in (runoff or {}, terrain_result or {}):
                for key in ("warnings", "assumptions"):
                    result[key] = list(dict.fromkeys(result[key] + source.get(key, [])))
            if structure_id:
                pond = {**existing, "id": structure_id, "kind": "detention_pond", "stage_storage": storage,
                        "outlets": outlet_specs, "initial_stage": initial,
                        "inflow_node": catchment["outlet"] if catchment else existing.get("inflow_node"),
                        "outflow_node": existing.get("outflow_node")}
                if surface_id:
                    pond.update(surface_id=surface_id, polygon=footprint, stages=levels)
                updated = cc.model_copy(entry)
                schema.upsert(updated, "structures", pond)
                cc.set_model(entry, updated)
                result["structure"] = pond
            return _finish(entry, doc, "detention", result, ["civil_hydrology", "civil_site_edit", "civil_load"])
        return cc.run_tool(run)

    @mcp.tool()
    def civil_culvert(culvert: Any = None, structure_id: Optional[str] = None,
                      flows: Any = None, tailwater_m: Optional[float] = None,
                      site: Site = None, doc_id: Optional[str] = None) -> str:
        """Evaluate HDS-5 controlling culvert headwater or a supplied flow-performance curve.

        UNITS: dimensions/headwater/tailwater m, total flow m3/s, velocity m/s; slope m/m.
        culvert is the engine's SI specification, or structure_id selects a site's culvert.
        flows=<number> solves once; flows=[ascending numbers] produces a performance curve;
        omitted flows uses culvert.flow_m3s or q_m3s. Explicit tailwater_m overrides the model.
        Tailwater is depth above outlet invert; road_crest_m/inlet_invert_m are absolute levels.
        Persists culvert results in civil_site, without editing the structure definition.
        Returns inlet/outlet HW, controlling regime and overtopping, or full curve points.
        Refuses missing tailwater/flow, unsupported entrance/geometry, invalid curve or a
        partly-full backwater case outside the engine approximation. No roadway flow partition.
        next_actions: civil_detention_route, civil_site_edit, civil_load.
        """
        def run():
            entry, doc = _context(site, doc_id)
            if culvert is None:
                if not structure_id:
                    raise CivilError("culvert_required", "Supply culvert or structure_id.")
                spec = schema.find(entry["model"], "structures", structure_id)
                if spec["kind"] != "culvert":
                    raise CivilError("structure_kind_mismatch", structure_id=structure_id, expected="culvert")
            else:
                spec = cc.as_obj(culvert)
            if not isinstance(spec, dict):
                raise CivilError("invalid_culvert")
            spec = dict(spec)
            tail = tailwater_m if tailwater_m is not None else spec.get("tailwater_m")
            if tail is None:
                raise CivilError("culvert_tailwater_required")
            spec["tailwater_m"] = tail
            values = cc.as_obj(flows) if isinstance(flows, (str, list, dict)) else flows
            if values is None:
                values = spec.get("flow_m3s", spec.get("q_m3s"))
            if values is None:
                raise CivilError("culvert_flow_required", "Supply flows or culvert.flow_m3s.")
            result = (culvert_engine.performance_curve(spec, values) if isinstance(values, list) else
                      culvert_engine.analyze(spec, values, tail))
            result["structure_id"] = structure_id
            return _finish(entry, doc, "culvert", result, ["civil_detention_route", "civil_site_edit", "civil_load"])
        return cc.run_tool(run)
