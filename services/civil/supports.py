"""Above-ground pipe support and rack-load planning, SI with piping mm conversion.

Euler–Bernoulli beam screening, NOT ASME B31 sustained/expansion acceptance.
No MSS span table is bundled: material/temperature properties and engineering
allowables are user inputs. Published pipe dimensions are resolved through the
existing fluids-backed piping catalog (MIT licensed).
"""
from __future__ import annotations

import bisect
import math
from numbers import Real

from services.piping.catalog import nearest_pipe

from .errors import CivilError

BEAM_REFERENCE = (
    "Euler–Bernoulli uniform-load beam: M=wL²/8, δ=5wL⁴/(384EI), "
    "Roark's Formulas for Stress and Strain, 8th ed. (2012), beam formulas. "
    "Continuous-span preliminary moment approximation M=wL²/10; "
    "deflection conservatively remains the simply-supported expression."
)
_GRAVITY_MS2 = 9.80665


def _number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise CivilError("invalid_input", f"{name} must be a finite number", field=name)
    value = float(value)
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise CivilError("invalid_input", f"{name} must be {'positive' if positive else 'nonnegative'}",
                         field=name)
    return value


def _result(name, *, warnings=None, assumptions=None, **values):
    return {"ok": True, **values, "warnings": warnings or [], "assumptions": assumptions or [],
            "method": {"name": name, "reference": BEAM_REFERENCE}}


def _point(value, name):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise CivilError("invalid_piping", f"{name} must be a three-coordinate mm point")
    return [_number(x, name) / 1000 for x in value]


def section_properties(od_m, wall_m):
    """Hollow circular section: A=π(Do²-Di²)/4; I=π(Do⁴-Di⁴)/64; Z=2I/Do.

    Reference: Roark's Formulas for Stress and Strain, 8th ed. (2012).
    Nominal straight-pipe wall; caller must deduct corrosion/tolerance if required.
    """
    od = _number(od_m, "od_m", positive=True)
    wall = _number(wall_m, "wall_m", positive=True)
    if 2*wall >= od:
        raise CivilError("invalid_pipe_section", "Require 0<wall<OD/2")
    inner = od-2*wall
    area = math.pi*(od*od-inner*inner)/4
    inertia = math.pi*(od**4-inner**4)/64
    return _result("annular_pipe_section", od_m=od, wall_m=wall, id_m=inner,
                   pipe_area_m2=area, contents_area_m2=math.pi*inner*inner/4,
                   moment_of_inertia_m4=inertia, section_modulus_m3=2*inertia/od)


def support_spacing(od_m, wall_m, elastic_modulus_gpa, allowable_stress_mpa, *,
                    pipe_density_kg_m3, fluid_density_kg_m3=1000.0,
                    insulation_kg_m=0.0, allowable_sag_mm=2.5,
                    condition="simply_supported", gravity_ms2=_GRAVITY_MS2):
    """Governing span=min(stress span, sag span), uniform dead load only.

    w=g*(ρpipe*Apipe+ρfluid*Abore+insulation_kg_m).
    Lstress=√(c*S*Z/w), c=8 simply_supported or 10 continuous.
    Lsag=(384*E*I*δallow/(5*w))^(1/4). Continuous uses the SAME conservative
    sag formula, not a claim of an exact continuous-beam deflection solution.
    S is the AVAILABLE bending stress budget, after pressure/other longitudinal
    stresses have been accounted for by the caller. No implicit B31 allowable.
    Reference: Euler–Bernoulli beam; Roark, 8th ed. (2012).
    """
    section = section_properties(od_m, wall_m)
    modulus = _number(elastic_modulus_gpa, "elastic_modulus_gpa", positive=True)*1e9
    stress = _number(allowable_stress_mpa, "allowable_stress_mpa", positive=True)*1e6
    density = _number(pipe_density_kg_m3, "pipe_density_kg_m3", positive=True)
    fluid = _number(fluid_density_kg_m3, "fluid_density_kg_m3", nonnegative=True)
    insulation = _number(insulation_kg_m, "insulation_kg_m", nonnegative=True)
    sag = _number(allowable_sag_mm, "allowable_sag_mm", positive=True)/1000
    gravity = _number(gravity_ms2, "gravity_ms2", positive=True)
    if condition not in {"simply_supported", "continuous"}:
        raise CivilError("unsupported_support_condition", "Use simply_supported or continuous")
    factor = 8 if condition == "simply_supported" else 10
    pipe_mass = density*section["pipe_area_m2"]
    contents_mass = fluid*section["contents_area_m2"]
    weight = gravity*(pipe_mass+contents_mass+insulation)
    inertia = section["moment_of_inertia_m4"]
    z = section["section_modulus_m3"]
    stress_span = math.sqrt(factor*stress*z/weight)
    sag_span = (384*modulus*inertia*sag/(5*weight))**0.25
    span = min(stress_span, sag_span)
    assumptions = ["Straight uniform pipe; all distributed weight treated as transverse for conservative span sizing.",
                   "Stress input is the available bending budget, not total code allowable.",
                   "Point loads, wind/seismic, vibration, support flexibility, local attachment and axial stress excluded."]
    if condition == "continuous":
        assumptions.append("M=wL²/10 is a preliminary continuous-run approximation; sag uses simply-supported upper bound.")
    return _result("pipe_beam_support_spacing", governing_span_m=span,
                   stress_span_m=stress_span, deflection_span_m=sag_span,
                   governing="stress" if stress_span <= sag_span else "deflection",
                   weight_n_m=weight, pipe_mass_kg_m=pipe_mass, contents_mass_kg_m=contents_mass,
                   insulation_mass_kg_m=insulation, stress_at_span_mpa=weight*span*span/factor/z/1e6,
                   sag_at_span_mm=5*weight*span**4/(384*modulus*inertia)*1000,
                   section={key: value for key, value in section.items()
                            if key not in {"ok", "warnings", "assumptions", "method"}},
                   condition=condition, assumptions=assumptions)


def _path(run):
    raw = run.get("path")
    if not isinstance(raw, list) or len(raw) < 2:
        raise CivilError("invalid_piping", "Run path requires at least two points", run_id=run.get("id"))
    points = [_point(p, f"run {run.get('id')} path") for p in raw]
    stations = [0.0]
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        if length == 0:
            raise CivilError("degenerate_path", "Consecutive run points must differ", run_id=run.get("id"))
        stations.append(stations[-1]+length)
    return points, stations


def _projections(point, points, stations, tolerance_m):
    matches = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        delta = [v-u for u, v in zip(a, b)]
        length = stations[i+1]-stations[i]
        t = sum((v-u)*d for v, u, d in zip(point, a, delta))/length**2
        # Finite-segment projection, not silently altering a user coordinate.
        bounded = min(1.0, max(0.0, t))
        projected = [u+bounded*d for u, d in zip(a, delta)]
        if math.dist(point, projected) <= tolerance_m:
            station = stations[i]+bounded*length
            if not any(abs(station-old) <= 1e-9 for old in matches):
                matches.append(station)
    return matches


def _location(station, points, stations):
    i = min(bisect.bisect_right(stations, station)-1, len(points)-2)
    fraction = (station-stations[i])/(stations[i+1]-stations[i])
    return [a+fraction*(b-a) for a, b in zip(points[i], points[i+1])]


def supports_for_piping(piping_doc, *, elastic_modulus_gpa, allowable_stress_mpa,
                        pipe_density_kg_m3, fluid_density_kg_m3=1000.0,
                        insulation_kg_m=0.0, allowable_sag_mm=2.5,
                        condition="simply_supported", run_properties=None,
                        fitting_weights_n=None, fitting_tolerance_mm=1.0):
    """Place tributary-weight supports along aicad.piping/1 centreline polylines.

    Endpoints, direction-changing vertices, connected branch endpoints, and every
    active fitting (including §9 valves/tees) are mandatory support stations. Fill
    the gaps at <= the beam governing span. One record per run/support station;
    coincident supports on different runs remain separate reactions, not duplicates
    to discard. Locations have explicit m/mm fields. Reactions sum to distributed
    run weight plus explicit fitting_weights_n, each fitting counted exactly once.

    run_properties maps run id to support_spacing keyword overrides. Geometry uses
    catalogued NPS/schedule OD/wall, never nominal NPS as physical diameter. Pipe
    length/weight follows the contract's polyline, not rounded swept elbow arcs.
    Fitting weights are optional user inputs (N), not invented catalog masses.
    Continuous condition changes spacing moment assumption only; reactions remain
    tributary deadweight, not an exact continuous structural analysis.
    """
    if not isinstance(piping_doc, dict) or piping_doc.get("schema") != "aicad.piping/1":
        raise CivilError("invalid_piping", "Expected aicad.piping/1 document")
    units = piping_doc.get("units", {"system": "MMGS"})
    if not isinstance(units, dict) or units.get("system") != "MMGS":
        raise CivilError("unsupported_piping_units", "aicad.piping/1 centreline points must be in mm")
    runs = piping_doc.get("runs")
    fittings = piping_doc.get("fittings", [])
    if not isinstance(runs, list) or not isinstance(fittings, list):
        raise CivilError("invalid_piping", "runs and fittings must be lists")
    overrides = {} if run_properties is None else run_properties
    weights = {} if fitting_weights_n is None else fitting_weights_n
    if not isinstance(overrides, dict) or not isinstance(weights, dict):
        raise CivilError("invalid_input", "run_properties and fitting_weights_n must be dicts")
    tolerance = _number(fitting_tolerance_mm, "fitting_tolerance_mm", nonnegative=True)/1000
    base = {"elastic_modulus_gpa": elastic_modulus_gpa, "allowable_stress_mpa": allowable_stress_mpa,
            "pipe_density_kg_m3": pipe_density_kg_m3, "fluid_density_kg_m3": fluid_density_kg_m3,
            "insulation_kg_m": insulation_kg_m, "allowable_sag_mm": allowable_sag_mm,
            "condition": condition}
    for key, value in base.items():
        if key != "condition":
            _number(value, key, positive=key in {"elastic_modulus_gpa", "allowable_stress_mpa", "pipe_density_kg_m3", "allowable_sag_mm"},
                    nonnegative=key in {"fluid_density_kg_m3", "insulation_kg_m"})
    if condition not in {"simply_supported", "continuous"}:
        raise CivilError("unsupported_support_condition", "Use simply_supported or continuous")
    active = {}
    all_run_ids = set()
    warnings = []
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("id"), str) or not run["id"]:
            raise CivilError("invalid_piping", "Each run requires an id")
        rid = run["id"]
        if rid in all_run_ids:
            raise CivilError("duplicate_id", "Duplicate piping run id", id=rid)
        all_run_ids.add(rid)
        if not isinstance(run.get("suppressed", False), bool):
            raise CivilError("invalid_piping", "suppressed must be boolean", run_id=rid)
        if run.get("suppressed", False):
            continue
        points, stations = _path(run)
        nps = _number(run.get("nps"), "nps", positive=True)
        schedule = run.get("schedule")
        if not isinstance(schedule, str):
            raise CivilError("invalid_piping", "schedule is required", run_id=rid)
        spec = nearest_pipe(nps=nps, schedule=schedule)
        if isinstance(spec, dict):
            raise CivilError("unknown_pipe_size", spec.get("reason"), run_id=rid)
        settings = overrides.get(rid, {})
        if not isinstance(settings, dict) or set(settings)-set(base):
            raise CivilError("invalid_input", "Run property overrides must use support_spacing property names", run_id=rid)
        analysis = support_spacing(spec.outer_dia/1000, spec.wall/1000, **(base | settings))
        if spec.nps != nps:
            warnings.append(f"{rid}: NPS {nps} resolved to catalogue NPS {spec.nps}.")
        landmarks = {0.0: {"endpoint"}, stations[-1]: {"endpoint"}}
        for i in range(1, len(points)-1):
            incoming = [(b-a)/(stations[i]-stations[i-1]) for a, b in zip(points[i-1], points[i])]
            outgoing = [(b-a)/(stations[i+1]-stations[i]) for a, b in zip(points[i], points[i+1])]
            if math.dist(incoming, outgoing) > 1e-10:
                landmarks[stations[i]] = {"bend"}
        active[rid] = {"points": points, "stations": stations, "landmarks": landmarks,
                       "analysis": analysis, "spec": spec, "point_loads": []}
    if set(overrides)-all_run_ids:
        raise CivilError("unknown_run", "run_properties references an unknown run")
    # Even an unannotated run joining the interior of another must be supported there.
    for rid, run in active.items():
        for other_id, other in active.items():
            if rid == other_id:
                continue
            for endpoint in (other["points"][0], other["points"][-1]):
                for station in _projections(endpoint, run["points"], run["stations"], tolerance):
                    run["landmarks"].setdefault(station, set()).add("branch")
    all_fitting_ids = set()
    active_fitting_ids = set()
    for fitting in fittings:
        if not isinstance(fitting, dict) or not isinstance(fitting.get("id"), str) or not fitting["id"]:
            raise CivilError("invalid_piping", "Each fitting requires an id")
        fid = fitting["id"]
        if fid in all_fitting_ids:
            raise CivilError("duplicate_id", "Duplicate fitting id", id=fid)
        all_fitting_ids.add(fid)
        if not isinstance(fitting.get("suppressed", False), bool):
            raise CivilError("invalid_piping", "suppressed must be boolean", fitting_id=fid)
        if fitting.get("suppressed", False):
            continue
        active_fitting_ids.add(fid)
        kind = fitting.get("type")
        if kind not in {"elbow", "tee", "reducer", "flange", "valve", "orifice", "custom"}:
            raise CivilError("unsupported_fitting", "Unknown piping fitting type", fitting_id=fid)
        origin = _point(fitting.get("origin"), f"fitting {fid} origin")
        params = fitting.get("params", {})
        if not isinstance(params, dict):
            raise CivilError("invalid_piping", "Fitting params must be a dict", fitting_id=fid)
        owner = fitting.get("run_id")
        refs = params.get("run_ids", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
            raise CivilError("invalid_piping", "params.run_ids must be a list of run ids", fitting_id=fid)
        branch = params.get("branch_run")
        required_refs = refs + ([branch] if branch is not None else []) + ([owner] if owner is not None else [])
        if any(not isinstance(ref, str) or ref not in all_run_ids for ref in required_refs):
            raise CivilError("unknown_run", "Fitting references an unknown run", fitting_id=fid)
        if owner is not None and owner not in active:
            if fid in weights and _number(weights[fid], f"fitting_weights_n.{fid}", nonnegative=True) != 0:
                raise CivilError("suppressed_run_load", "Active fitting weight cannot belong to a suppressed run", fitting_id=fid)
            warnings.append(f"{fid}: fitting on suppressed owner run excluded.")
            continue
        matches = []
        for rid, run in active.items():
            projections = _projections(origin, run["points"], run["stations"], tolerance)
            if rid in required_refs and not projections:
                raise CivilError("fitting_not_on_run", "Referenced fitting must lie on the run centreline", fitting_id=fid, run_id=rid)
            for station in projections:
                run["landmarks"].setdefault(station, set()).add(f"fitting:{fid}:{kind}")
                matches.append((rid, station))
        if not matches:
            raise CivilError("fitting_not_on_run", "Active fitting has no run within fitting_tolerance_mm", fitting_id=fid)
        if len({rid for rid, _ in matches}) < len(matches):
            raise CivilError("ambiguous_fitting_station", "Fitting lies at multiple stations of one self-crossing run", fitting_id=fid)
        weight = _number(weights.get(fid, 0.0), f"fitting_weights_n.{fid}", nonnegative=True)
        selected = next((item for item in matches if item[0] == owner), matches[0])
        active[selected[0]]["point_loads"].append((selected[1], weight, fid))
    if set(weights)-active_fitting_ids:
        raise CivilError("unknown_fitting", "fitting_weights_n references unknown or suppressed fittings")
    support_list = []
    summaries = {}
    total_weight = 0.0
    for rid, run in active.items():
        span = run["analysis"]["governing_span_m"]
        mandatory = sorted(run["landmarks"])
        marks = []
        for start, end in zip(mandatory, mandatory[1:]):
            if not marks:
                marks.append(start)
            pieces = max(1, math.ceil((end-start)/span))
            marks.extend(start+(end-start)*j/pieces for j in range(1, pieces+1))
        # Coincident mathematical station calculations can differ at floating precision.
        unique_marks = []
        for station in marks:
            if unique_marks and abs(station-unique_marks[-1]) <= 1e-9:
                continue
            unique_marks.append(station)
        marks = unique_marks
        weight_per_m = run["analysis"]["weight_n_m"]
        local = []
        for index, station in enumerate(marks):
            tributary = ((station-marks[index-1])/2 if index else 0) + (
                        (marks[index+1]-station)/2 if index+1 < len(marks) else 0)
            reasons = set()
            for landmark, labels in run["landmarks"].items():
                if abs(landmark-station) <= 1e-9:
                    reasons.update(labels)
            location = _location(station, run["points"], run["stations"])
            point_weights = [(w, fid) for s, w, fid in run["point_loads"] if abs(s-station) <= 1e-9]
            record = {"id": f"{rid}:support:{index+1}", "run_id": rid, "station_m": station,
                      "station_mm": station*1000, "location_m": location,
                      "location_mm": [p*1000 for p in location], "type": "gravity_support",
                      "reasons": sorted(reasons) if reasons else ["spacing"],
                      "tributary_length_m": tributary, "reaction_n": weight_per_m*tributary+sum(w for w, _ in point_weights),
                      "fitting_weight_n": sum(w for w, _ in point_weights),
                      "loaded_fitting_ids": [fid for _, fid in point_weights if fid in weights],
                      "weight_n_m": weight_per_m}
            local.append(record)
        run_weight = weight_per_m*run["stations"][-1]+sum(w for _, w, _ in run["point_loads"])
        total_weight += run_weight
        support_list.extend(local)
        summaries[rid] = {"length_m": run["stations"][-1], "weight_n": run_weight,
                          "governing_span_m": span, "governing": run["analysis"]["governing"],
                          "weight_n_m": weight_per_m, "od_mm": run["spec"].outer_dia,
                          "wall_mm": run["spec"].wall, "resolved_nps": run["spec"].nps,
                          "support_count": len(local)}
    if active_fitting_ids-set(weights):
        warnings.append("Unspecified fitting self-weights are excluded; provide fitting_weights_n for valves/fittings.")
    if any(any(abs(a[2]-b[2]) > 1e-9 for a, b in zip(run["points"], run["points"][1:])) for run in active.values()):
        warnings.append("Inclined/vertical runs use full-weight transverse span screening; riser axial stability/anchors not evaluated.")
    return _result("piping_polyline_tributary_supports", supports=support_list, runs=summaries,
                   total_weight_n=total_weight, total_reaction_n=sum(s["reaction_n"] for s in support_list),
                   warnings=warnings, assumptions=["Polyline lengths in mm explicitly converted to m; rounded sweep take-outs are not deducted.",
                   "Every mandatory landmark is supported; uniform-weight tributary reactions, no exact continuous-beam reactions.",
                   "Coincident supports on separate runs carry separate tributary contributions.",
                   "Properties and stress budgets are caller inputs at the operating temperature; no ASME/MSS code acceptance."])


def rack_loads(supports, *, friction_coefficient, beam_assignments=None):
    """Sum vertical support reactions and Coulomb sliding-friction envelopes by beam.

    supports is a support list or supports_for_piping result. Assign each support
    by beam_assignments={support_id: beam_id} or its beam_id field. Friction is
    μ ΣR, an unsigned worst-direction thermal sliding load envelope, NOT an
    expansion stress analysis or a claim that static friction is fully mobilized.
    Reference: Coulomb friction; tributary equilibrium; ASME B31 flexibility
    and actual rack-frame design remain separate analyses.
    """
    mu = _number(friction_coefficient, "friction_coefficient", nonnegative=True)
    if isinstance(supports, dict):
        supports = supports.get("supports")
    if not isinstance(supports, list):
        raise CivilError("invalid_input", "supports must be a list or supports result dict")
    if beam_assignments is not None and not isinstance(beam_assignments, dict):
        raise CivilError("invalid_input", "beam_assignments must be a dict")
    assignments = {} if beam_assignments is None else beam_assignments
    beams = {}
    ids = set()
    for support in supports:
        if not isinstance(support, dict) or not isinstance(support.get("id"), str):
            raise CivilError("invalid_input", "Each support needs an id")
        sid = support["id"]
        if sid in ids:
            raise CivilError("duplicate_id", "Duplicate support id", id=sid)
        ids.add(sid)
        beam = assignments.get(sid, support.get("beam_id"))
        if not isinstance(beam, str) or not beam:
            raise CivilError("rack_assignment_required", "Assign each support to a rack beam", support_id=sid)
        reaction = _number(support.get("reaction_n"), "reaction_n", nonnegative=True)
        row = beams.setdefault(beam, {"beam_id": beam, "support_ids": [], "vertical_load_n": 0.0,
                                      "thermal_friction_n": 0.0})
        row["support_ids"].append(sid)
        row["vertical_load_n"] += reaction
        row["thermal_friction_n"] += mu*reaction
    if set(assignments)-ids:
        raise CivilError("unknown_support", "beam_assignments references an unknown support")
    out = _result("rack_tributary_and_coulomb_loads", beams=beams, friction_coefficient=mu,
                  total_vertical_load_n=sum(b["vertical_load_n"] for b in beams.values()),
                  total_thermal_friction_n=sum(b["thermal_friction_n"] for b in beams.values()),
                  assumptions=["All friction contributions conservatively act in the same direction on each beam.",
                               "Thermal-friction value is a sliding bound μR, not an elastic thermal reaction.",
                               "No rack-beam stress, column/frame, wind, seismic or anchor design is performed."])
    out["method"]["reference"] = "Tributary static equilibrium; Coulomb friction F=μR; ASME B31 flexibility assessment separate"
    return out
