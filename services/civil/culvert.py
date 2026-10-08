"""FHWA HDS-5 (3rd edition, 2012) conventional culvert calculations in SI.

Inlet coefficients are transcribed from Appendix A Table A.1, not Table C.1
(which contains natural-channel roughness). Entrance loss coefficients are from
Table C.2. Appendix A specifies an empirical transition tangent to the weir and
orifice curves; this implementation uses a slope-matched cubic Hermite bridge
between the documented limits Ku Q/(A sqrt(D))=3.5 and 4.0. It is not HY-8's
historic fifth-degree regression and is explicitly identified in results.
Outlet control uses equations 3.5/3.6 and the extended hydraulic-grade-line
approximation (§3.1.4) where partly full. Below 0.75D it refuses rather than
presenting that approximation as a valid backwater calculation.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from scipy.optimize import brentq

from .errors import CivilError

_G = 9.80665
_KU = 1.811
_REFERENCE = "FHWA-HIF-12-026, HDS-5 Hydraulic Design of Highway Culverts, Third Edition (April 2012), Appendix A eqs. A.1–A.3 / Table A.1; §3.1.4 eqs. 3.5–3.6 / Appendix C Table C.2; https://www.fhwa.dot.gov/engineering/hydraulics/pubs/12026/hif12026.pdf"


def _result(name, *, warnings=None, assumptions=None, **values):
    return {"ok": True, **values, "warnings": list(warnings or []),
            "assumptions": list(assumptions or []), "method": {"name": name, "reference": _REFERENCE}}


def _number(value, field, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise CivilError("invalid_culvert_parameter", field=field)
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CivilError("invalid_culvert_parameter", field=field) from exc
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        raise CivilError("invalid_culvert_parameter", field=field)
    return number


@lru_cache(maxsize=1)
def _inlets():
    return json.loads(Path(__file__).with_name("data").joinpath("culvert_hds5_coefficients.json").read_text())["inlets"]


def inlet_types():
    """List supported entrances and their HDS-5 A.1 / C.2 coefficients."""
    return _result("HDS-5 entrance coefficient inventory", inlets={key: dict(value) for key, value in _inlets().items()})


def _parameters(culvert):
    if not isinstance(culvert, dict):
        raise CivilError("invalid_culvert")
    entrance = culvert.get("inlet")
    if not isinstance(entrance, str) or entrance not in _inlets():
        raise CivilError("unsupported_culvert_inlet", "Use inlet_types() for supported HDS-5 configurations", inlet=entrance)
    coefficients = _inlets()[entrance]
    shape = culvert.get("shape")
    if shape != coefficients["shape"]:
        raise CivilError("culvert_inlet_shape_mismatch", inlet=entrance, shape=shape)
    if shape == "circular":
        height = _number(culvert.get("diameter_m"), "diameter_m", positive=True)
        width = height
        area, perimeter = math.pi * height ** 2 / 4, math.pi * height
    elif shape == "rectangular":
        height = _number(culvert.get("height_m"), "height_m", positive=True)
        width = _number(culvert.get("width_m"), "width_m", positive=True)
        area, perimeter = height * width, 2 * (height + width)
    else:
        raise CivilError("unsupported_culvert_shape", shape=shape)
    length = _number(culvert.get("length_m"), "length_m", positive=True)
    slope = _number(culvert.get("slope"), "slope")
    n = _number(culvert.get("n"), "n", positive=True)
    barrels = _number(culvert.get("barrels", 1), "barrels", positive=True)
    if not barrels.is_integer():
        raise CivilError("invalid_culvert_parameter", field="barrels")
    ke = coefficients["ke"] if "ke" not in culvert else _number(culvert["ke"], "ke", nonnegative=True)
    return {"shape": shape, "height": height, "width": width, "area": area, "radius": area / perimeter,
            "length": length, "slope": slope, "n": n, "barrels": int(barrels), "ke": ke,
            "coefficients": coefficients, "entrance": entrance}


def _circle_area(theta, diameter):
    # Series avoids subtractive cancellation near a dry circular section.
    difference = theta ** 3 / 6 - theta ** 5 / 120 + theta ** 7 / 5040 if theta < 1e-3 else theta - math.sin(theta)
    return diameter ** 2 * difference / 8


def _critical(p, q):
    if q == 0:
        return 0.0, 0.0, 0.0
    if p["shape"] == "rectangular":
        depth = (q ** 2 / (_G * p["width"] ** 2)) ** (1 / 3)
        area = p["width"] * depth
    else:
        diameter = p["height"]
        def equation(theta):
            area = _circle_area(theta, diameter)
            top_width = diameter * math.sin(theta / 2)
            return _G * area ** 3 - q ** 2 * top_width
        theta = brentq(equation, 1e-10, 2 * math.pi - 1e-10, xtol=1e-12)
        area = _circle_area(theta, diameter)
        depth = diameter * math.sin(theta / 4) ** 2
    energy = depth + (q / area) ** 2 / (2 * _G)
    return depth, energy, area


def _unsubmerged(p, x):
    coefficients, height = p["coefficients"], p["height"]
    q = x * p["area"] * math.sqrt(height) / _KU
    if coefficients["form"] == 1:
        _, energy, critical_area = _critical(p, q)
        value = energy / height + coefficients["K"] * x ** coefficients["M"] + coefficients["Ks"] * p["slope"]
        derivative_energy = q * p["area"] / (_G * critical_area ** 2 * _KU * math.sqrt(height))
        derivative = derivative_energy + coefficients["K"] * coefficients["M"] * x ** (coefficients["M"] - 1)
    else:
        value = coefficients["K"] * x ** coefficients["M"]
        derivative = coefficients["K"] * coefficients["M"] * x ** (coefficients["M"] - 1)
    return value, derivative


def _inlet(p, q):
    if q == 0:
        return 0.0, "no_flow", 0.0
    coefficients, height = p["coefficients"], p["height"]
    x = _KU * q / (p["area"] * math.sqrt(height))
    if x <= 3.5:
        ratio, _ = _unsubmerged(p, x)
        regime = "unsubmerged_form_" + str(coefficients["form"])
    elif x >= 4:
        ratio = coefficients["c"] * x ** 2 + coefficients["Y"] + coefficients["Ks"] * p["slope"]
        regime = "submerged"
    else:
        y0, slope0 = _unsubmerged(p, 3.5)
        y1 = coefficients["c"] * 4 ** 2 + coefficients["Y"] + coefficients["Ks"] * p["slope"]
        slope1 = 8 * coefficients["c"]
        u = (x - 3.5) / 0.5
        ratio = ((2 * u ** 3 - 3 * u ** 2 + 1) * y0
                 + (u ** 3 - 2 * u ** 2 + u) * 0.5 * slope0
                 + (-2 * u ** 3 + 3 * u ** 2) * y1
                 + (u ** 3 - u ** 2) * 0.5 * slope1)
        regime = "transition_hermite"
    if ratio < 0:
        raise CivilError("inlet_equation_outside_domain", "Slope correction exceeds computed inlet head; HDS-5 equation is not valid for this low-flow/steep-slope case")
    return ratio * height, regime, x


def inlet_control(culvert, q_m3s):
    """HDS-5 inlet headwater above inlet invert (Appendix A).

    culvert: shape=circular (diameter_m) or rectangular (height_m,width_m),
    inlet (from inlet_types), length_m,slope,n and optional integer barrels=1.
    Total q_m3s divides equally among identical barrels. Form 2 unsubmerged
    equation has no slope correction, as printed in A.2; submerged always uses
    Ks*S. Cubic Hermite transition is slope-matched, not a claimed HY-8 match.
    """
    p = _parameters(culvert)
    total = _number(q_m3s, "q_m3s", nonnegative=True)
    q = total / p["barrels"]
    hw, regime, x = _inlet(p, q)
    assumptions = ["Identical barrels share total flow equally; no blockage, embedment, tapered inlet, skew or approach velocity.",
                   "Ku=1.811 for SI; Ks=-0.5 except mitered CMP +0.7; actual barrel slope used."]
    warnings = []
    if regime == "transition_hermite":
        assumptions.append("Empirical transition: cubic Hermite, tangent to equations at Ku Q/(A√D)=3.5 and 4.0; not the historical HY-8 polynomial.")
        warnings.append("inlet_transition_is_empirical_interpolation")
    if "barrels" not in culvert:
        assumptions.append("One barrel (documented default).")
    return _result("HDS-5 inlet control", headwater_m=hw, inlet_control_hw_m=hw, inlet_regime=regime,
                   flow_per_barrel_m3s=q, normalized_flow=x, units={"normalized_flow": "US customary Q/(A sqrt(D)), converted by Ku=1.811"},
                   coefficients=dict(p["coefficients"]), warnings=warnings, assumptions=assumptions)


def _outlet(p, q, tailwater):
    if q == 0:
        # Static pool depth is zero where downstream water is below the inlet invert.
        return {"headwater_m": max(0.0, tailwater - p["length"] * p["slope"]),
                "critical_depth_m": 0.0, "outlet_reference_depth_m": tailwater,
                "headloss_m": 0.0, "velocity_ms": 0.0, "outlet_regime": "static_no_flow"}, [], ["At zero flow, hydrostatic tailwater reaches the inlet only if above the inlet invert."]
    depth, _, _ = _critical(p, q)
    warnings, assumptions = [], []
    if p["shape"] == "rectangular" and depth > p["height"]:
        # A free-surface critical section deeper than the roof is impossible.
        critical_output = None
        reference_depth = max(tailwater, p["height"])
        assumptions.append("Calculated rectangular free-surface critical depth exceeds the roof; barrel is pressurized, outlet reference is max(TW,D).")
    else:
        critical_output = depth
        reference_depth = max(tailwater, (depth + p["height"]) / 2)
    velocity = q / p["area"]
    velocity_head = velocity ** 2 / (2 * _G)
    friction_factor = 2 * _G * p["n"] ** 2 * p["length"] / p["radius"] ** (4 / 3)
    loss = (1 + p["ke"] + friction_factor) * velocity_head
    hw = loss + reference_depth - p["length"] * p["slope"]
    if hw < 0.75 * p["height"]:
        raise CivilError("partly_full_backwater_required", "HDS-5 extended-HGL approximation is not accurate below HW=0.75D; use a free-surface backwater solver", approximate_headwater_m=hw, minimum_approximation_headwater_m=0.75 * p["height"])
    if tailwater < p["height"] or hw < p["height"]:
        regime = "partly_full_extended_hgl"
        warnings.append("outlet_control_partly_full_extended_hgl_approximation")
        assumptions.append("HDS-5 §3.1.4 extended full-flow HGL, ho=max(TW,(dc+D)/2); approximate for partly full flow and refused below HW=0.75D.")
    else:
        regime = "full_flow"
    return {"headwater_m": hw, "critical_depth_m": critical_output,
            "outlet_reference_depth_m": reference_depth, "headloss_m": loss,
            "velocity_ms": velocity, "outlet_regime": regime}, warnings, assumptions


def outlet_control(culvert, q_m3s, tailwater_m):
    """HDS-5 outlet HW=H+ho-LS using full-barrel Manning losses in SI.

    H=(1+ke+2g n² L/R^(4/3))V²/(2g), ho=max(TW,(dc+D)/2).
    Tailwater is depth above outlet invert, not an absolute elevation. Full flow
    is exact for the stated assumptions; partly full uses the published HGL
    approximation and explicitly refuses below its 0.75D applicability boundary.
    """
    p = _parameters(culvert)
    total = _number(q_m3s, "q_m3s", nonnegative=True)
    tailwater = _number(tailwater_m, "tailwater_m", nonnegative=True)
    values, warnings, assumptions = _outlet(p, total / p["barrels"], tailwater)
    assumptions += ["Approach and downstream velocity heads neglected; exit loss coefficient=1; uniform Manning n and full wetted barrel perimeter.",
                    "Equal flow in identical barrels; no additional bend or junction losses."]
    if "ke" in culvert:
        assumptions.append("Entrance ke explicitly supplied by user instead of Table C.2.")
    if "barrels" not in culvert:
        assumptions.append("One barrel (documented default).")
    return _result("HDS-5 outlet control", **values, outlet_control_hw_m=values["headwater_m"],
                   flow_per_barrel_m3s=total / p["barrels"], ke=p["ke"], warnings=warnings, assumptions=assumptions)


def analyze(culvert, q_m3s, tailwater_m):
    """Return controlling inlet/outlet headwater and road overtopping check.

    Model fields are documented by inlet_control. To check road overtopping,
    supply both inlet_invert_m and road_crest_m as absolute elevations in the
    same datum. Without road_crest_m, overtopping is null and a warning is
    returned. The prescribed Q goes through the culvert; roadway overflow is
    flagged, not silently diverted or claimed to be routed over the road.
    """
    inlet = inlet_control(culvert, q_m3s)
    outlet = outlet_control(culvert, q_m3s, tailwater_m)
    hw = max(inlet["headwater_m"], outlet["headwater_m"])
    controlling = "inlet" if inlet["headwater_m"] >= outlet["headwater_m"] else "outlet"
    warnings = inlet["warnings"] + outlet["warnings"]
    assumptions = inlet["assumptions"] + outlet["assumptions"]
    elevation, overtopping = None, None
    if "inlet_invert_m" in culvert:
        elevation = _number(culvert["inlet_invert_m"], "inlet_invert_m") + hw
    if culvert.get("road_crest_m") is not None:
        if elevation is None:
            raise CivilError("culvert_inlet_datum_required", "Supply inlet_invert_m with road_crest_m")
        crest = _number(culvert["road_crest_m"], "road_crest_m")
        if crest <= float(culvert["inlet_invert_m"]):
            raise CivilError("invalid_road_crest", "Road crest must be above inlet invert")
        overtopping = elevation > crest
        if overtopping:
            warnings.append("roadway_overtopping_flow_not_partitioned")
    else:
        warnings.append("road_crest_not_supplied_overtopping_not_checked")
    return _result("HDS-5 controlling culvert headwater", flow_m3s=float(q_m3s), tailwater_m=float(tailwater_m),
                   inlet_control_hw_m=inlet["headwater_m"], outlet_control_hw_m=outlet["headwater_m"],
                   headwater_m=hw, headwater_elevation_m=elevation, controlling=controlling,
                   inlet_regime=inlet["inlet_regime"], outlet_regime=outlet["outlet_regime"],
                   critical_depth_m=outlet["critical_depth_m"], outlet_velocity_ms=outlet["velocity_ms"],
                   headloss_m=outlet["headloss_m"], overtopping=overtopping,
                   warnings=list(dict.fromkeys(warnings)), assumptions=list(dict.fromkeys(assumptions)))


def performance_curve(culvert, flows):
    """HDS-5 headwater performance at supplied total flows and constant tailwater.

    ``culvert.tailwater_m`` is required (depth above outlet invert). Input flows
    must be nonnegative, strictly increasing; unsupported low-flow backwater
    cases are refused, not dropped from the curve.
    """
    if not isinstance(culvert, dict) or "tailwater_m" not in culvert:
        raise CivilError("culvert_tailwater_required")
    if not isinstance(flows, (list, tuple)) or not flows:
        raise CivilError("invalid_performance_flows")
    flows = [_number(q, "flows", nonnegative=True) for q in flows]
    if any(q1 >= q2 for q1, q2 in zip(flows, flows[1:])):
        raise CivilError("invalid_performance_flows", "Flows must be strictly increasing")
    points = [analyze(culvert, q, culvert["tailwater_m"]) for q in flows]
    return _result("HDS-5 culvert performance curve", points=points,
                   warnings=list(dict.fromkeys(w for point in points for w in point["warnings"])),
                   assumptions=["Constant user-supplied tailwater for all curve points; no roadway flow split or storage routing."])
