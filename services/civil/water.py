"""SI distribution hydraulics using the Todini–Pilati Global Gradient Algorithm.

References: EPANET 2.2 User Manual (EPA/600/R-20/133), §§3.1, 12.1 and
Appendix D; Todini & Pilati (1988), A gradient algorithm for the analysis of
pipe networks; Wagner, Shamir & Marks (1988), Water distribution reliability.
The solver is independent Python/SciPy, not an EPANET toolkit wrapper. Source
coefficient/transition/status equations were consulted in OWA EPANET v2.2
(MIT): https://github.com/OpenWaterAnalytics/EPANET/tree/v2.2/src .

EPS uses piecewise-constant hydraulics and exact Euler cylinder-volume updates,
truncating steps at pattern, level-control, tank-limit and elapsed-time events.
Unlike EPANET's integer-second event rounding, events retain fractional seconds.
Quality, rules, emitters, constant-power pumps and non-cylindrical tanks are not
implemented and are explicitly refused when hydraulically relevant.
"""
from __future__ import annotations

from copy import deepcopy
import math
import warnings as _warnings

import numpy as np
from scipy.sparse import bmat, coo_matrix, diags
from scipy.sparse.linalg import MatrixRankWarning, spsolve
from fluids.friction import friction_factor

from .errors import CivilError

_G = 9.80665
_HW = 4.727 * 0.3048 ** (4.871 - 3 * 1.852)
# EPANET RQtol=1e-7 ft/(cfs) expressed as m/(m3/s).
_GMIN = 1e-7 / 0.3048 ** 2
_HTOL = 0.0005 * 0.3048
_QTOL = 0.0001 * 0.3048 ** 3
_DEFAULTS = {"duration_s": 0, "hydraulic_step_s": 3600, "pattern_step_s": 3600,
             "demand_model": "DDA", "p_min_m": 0.0, "p_req_m": 20.0,
             "pda_exponent": 0.5, "viscosity_m2s": 1.004e-6,
             "accuracy": 1e-6, "max_iter": 200}
_METHOD = {"name": "Todini–Pilati global gradient (sparse nodal Schur complement)",
           "reference": "EPANET 2.2 User Manual, EPA/600/R-20/133, §12.1 and Appendix D; Wagner et al. (1988) PDA"}


def _number(value, reason, *, minimum=None, positive=False):
    if isinstance(value, bool):
        raise CivilError(reason, "A finite numeric value is required.")
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CivilError(reason, "A finite numeric value is required.") from exc
    if not math.isfinite(value) or (positive and value <= 0) or (minimum is not None and value < minimum):
        raise CivilError(reason, "Value is non-finite or outside its physical range.")
    return value


def _pump_fit(points):
    """EPANET one-point parabola, three-point power law, or linear segments."""
    if not isinstance(points, list) or not points:
        raise CivilError("invalid_pump_curve", "Provide a decreasing flow/head curve.")
    try:
        p = [(_number(row[0], "invalid_pump_curve", minimum=0),
              _number(row[1], "invalid_pump_curve", minimum=0)) for row in points if len(row) == 2]
    except (TypeError, IndexError) as exc:
        raise CivilError("invalid_pump_curve") from exc
    if len(p) != len(points) or any(p[i][0] >= p[i+1][0] or p[i][1] <= p[i+1][1] for i in range(len(p)-1)):
        raise CivilError("invalid_pump_curve", "Flow must increase strictly and head decrease strictly.")
    if len(p) == 1:
        q, h = p[0]
        if q <= 0 or h <= 0:
            raise CivilError("invalid_pump_curve")
        # EPANET adds (0,1.33333 H) and (2Q,0), then fits a power law.
        a = 1.33333 * h
        exponent = math.log(a / (a-h)) / math.log(2.0)
        return ("power", a, (a-h) / q ** exponent, exponent)
    if len(p) == 3:
        (q0, h0), (q1, h1), (q2, h2) = p
        if q0 != 0 or h0 <= 0:
            raise CivilError("invalid_pump_curve", "A three-point EPANET power curve must start at zero flow.")
        exponent = math.log((h0-h2)/(h0-h1)) / math.log(q2/q1)
        if not math.isfinite(exponent) or exponent <= 0:
            raise CivilError("invalid_pump_curve")
        return ("power", h0, (h0-h1)/q1**exponent, exponent)
    return ("linear", p)


def _pump_loss(fit, q, speed):
    if fit[0] == "power":
        _, shutoff, resistance, exponent = fit
        r = resistance * speed ** (2-exponent)
        gradient = max(exponent*r*abs(q)**(exponent-1) if q != 0 else _GMIN, _GMIN)
        loss = -shutoff*speed**2 + (gradient*q if gradient == _GMIN else r*q*abs(q)**(exponent-1))
        return loss, gradient, shutoff*speed**2
    points = fit[1]
    design_q = q/speed
    index = int(np.searchsorted([p[0] for p in points], design_q, side="right"))
    index = min(max(index, 1), len(points)-1)
    q0, h0 = points[index-1]
    q1, h1 = points[index]
    slope = (h1-h0)/(q1-q0)
    intercept = h0-slope*q0
    first_slope = (points[1][1]-points[0][1])/(points[1][0]-points[0][0])
    shutoff = points[0][1]-first_slope*points[0][0]
    return -intercept*speed**2-slope*speed*q, -slope*speed, shutoff*speed**2


def _darcy_factor(reynolds, relative_roughness):
    """Swamee–Jain above 4000, Dunlop cubic 2000–4000 (EPANET 2.2)."""
    if reynolds <= 2000:
        return 64/reynolds, -64/reynolds**2
    e = relative_roughness
    if reynolds >= 4000:
        # fluids 1.3 spells the Swamee–Jain term (6.97/Re)^0.9;
        # EPANET uses 5.74/Re^0.9. Their rounded constants differ by ~1e-6
        # in f. Rescale the helper argument to evaluate EPANET's exact term,
        # keeping the headloss and its analytic derivative consistent.
        helper_re = reynolds * (6.97 / 5.74 ** (1 / 0.9))
        f = friction_factor(Re=helper_re, eD=e, Method="Swamee_Jain_1976")
        y = e/3.7+5.74/reynolds**0.9
        derivative = 2.0*0.25*0.9*5.74/(reynolds**1.9*math.log(10)*y*math.log10(y)**3)
        return f, derivative
    # Dunlop interpolation is not an arbitrary interpolation of endpoint f.
    a9 = -2/math.log(10)
    ab = 5.74/4000**0.9
    ac = -2*0.9*2/math.log(10)*ab
    y2, y3 = e/3.7+ab, a9*math.log(e/3.7+ab)
    fa = 1/y3**2
    fb = (2+ac/(y2*y3))*fa
    x1, x2 = 7*fa-fb, 0.128-17*fa+2.5*fb
    x3, x4 = -0.128+13*fa-2*fb, 0.032-3*fa+0.5*fb
    r = reynolds/2000
    return x1+r*(x2+r*(x3+r*x4)), (x2+r*(2*x3+3*r*x4))/2000


def _pipe_loss(pipe, q, formula, viscosity):
    d, length, roughness = pipe["diameter"], pipe["length"], pipe["roughness"]
    area = math.pi*d*d/4
    minor = pipe.get("minor_loss", 0)/(2*_G*area**2)
    aq = abs(q)
    if formula == "H-W":
        r = _HW*length/(roughness**1.852*d**4.871)
        gradient = 1.852*r*aq**0.852
        if gradient < _GMIN:
            loss, gradient = _GMIN*q, _GMIN
        else:
            loss = r*q*aq**0.852
        return loss+minor*q*aq, gradient+2*minor*aq
    r = length/(2*_G*d*area**2)
    re_per_q = d/(area*viscosity)
    if aq*re_per_q <= 2000:
        laminar = 64*r/re_per_q
        return q*(laminar+minor*aq), laminar+2*minor*aq
    f, df_dre = _darcy_factor(aq*re_per_q, roughness/d)
    return (f*r+minor)*q*aq, 2*(f*r+minor)*aq+df_dre*re_per_q*r*aq**2


def _prepare(network):
    if not isinstance(network, dict):
        raise CivilError("invalid_network")
    net = deepcopy(network)
    for collection in ("junctions", "reservoirs", "tanks", "pipes", "pumps", "valves"):
        if net.get(collection) is None:
            net[collection] = []
    for collection in ("patterns", "options", "curves"):
        if net.get(collection) is None:
            net[collection] = {}
    if not isinstance(net.get("options", {}), dict):
        raise CivilError("invalid_options", "options must be an object.")
    for collection in ("junctions", "reservoirs", "tanks", "pipes", "pumps", "valves", "controls", "epanet_passthrough"):
        values = net.get(collection, [])
        if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
            raise CivilError("invalid_network", collection=collection)
    if not isinstance(net.get("patterns", {}), dict):
        raise CivilError("invalid_pattern", "patterns must map IDs to multiplier lists.")
    options = dict(_DEFAULTS)
    options.update(net.get("options", {}))
    formula = str(net.get("headloss", "H-W")).upper()
    if formula not in ("H-W", "D-W"):
        raise CivilError("unsupported_headloss", headloss=formula)
    for entry in net.get("epanet_passthrough", []):
        if entry.get("active") and entry.get("hydraulic"):
            raise CivilError("unsupported_epanet_hydraulics", section=entry["section"])
    if str(options.get("demand_model", "DDA")).upper() not in ("DDA", "PDA"):
        raise CivilError("invalid_demand_model")
    options["demand_model"] = str(options["demand_model"]).upper()
    for key in ("duration_s", "pattern_start_s", "demand_multiplier"):
        if key in options:
            options[key] = _number(options[key], "invalid_options", minimum=0)
    for key in ("hydraulic_step_s", "pattern_step_s", "viscosity_m2s", "accuracy", "pda_exponent"):
        options[key] = _number(options[key], "invalid_options", positive=True)
    for key in ("p_min_m", "p_req_m"):
        options[key] = _number(options[key], "invalid_options")
    if options["demand_model"] == "PDA" and options["p_req_m"] <= options["p_min_m"]:
        raise CivilError("invalid_pda_pressure_range")
    count = _number(options["max_iter"], "invalid_options", positive=True)
    if count != int(count):
        raise CivilError("invalid_options", "max_iter must be an integer.")
    options["max_iter"] = int(count)
    nodes, junctions, fixed, tanks = {}, [], [], {}
    for collection in ("junctions", "reservoirs", "tanks"):
        for node in net.get(collection, []):
            ident = node.get("id")
            if not isinstance(ident, str) or not ident or ident in nodes:
                raise CivilError("invalid_node_id", element_id=ident)
            node["_kind"] = collection
            nodes[ident] = node
            if collection == "junctions":
                node["elevation"] = _number(node.get("elevation"), "invalid_junction")
                node["demand_m3s"] = _number(node.get("demand_m3s", 0), "invalid_junction")
                node["_fixed_demand_m3s"] = _number(node.get("_fixed_demand_m3s", 0), "invalid_junction")
                if node.get("emitter") or node.get("emitter_coefficient"):
                    raise CivilError("unsupported_emitters", element_id=ident)
                if not isinstance(node.get("demands", []), list) or any(not isinstance(d, dict) for d in node.get("demands", [])):
                    raise CivilError("invalid_demand", element_id=ident)
                for demand in node.get("demands", []):
                    demand["demand_m3s"] = _number(demand.get("demand_m3s"), "invalid_demand")
                junctions.append(ident)
            else:
                fixed.append(ident)
                if collection == "reservoirs":
                    node["head"] = _number(node.get("head"), "invalid_reservoir")
                else:
                    if node.get("volume_curve"):
                        raise CivilError("unsupported_tank_volume_curve", element_id=ident)
                    if node.get("overflow"):
                        raise CivilError("unsupported_tank_overflow", element_id=ident)
                    for key in ("elevation", "init_level", "min_level", "max_level"):
                        node[key] = _number(node.get(key), "invalid_tank")
                    node["diameter"] = _number(node.get("diameter"), "invalid_tank", positive=True)
                    if not 0 <= node["min_level"] <= node["init_level"] <= node["max_level"] or node["min_level"] == node["max_level"]:
                        raise CivilError("invalid_tank_levels", element_id=ident)
                    tanks[ident] = node
    if not nodes or not fixed:
        raise CivilError("no_head_boundary", "A water network requires at least one reservoir or tank.")
    patterns = net.get("patterns", {})
    for ident, values in patterns.items():
        if not isinstance(values, list) or not values:
            raise CivilError("invalid_pattern", element_id=ident)
        patterns[ident] = [_number(v, "invalid_pattern", minimum=0) for v in values]
    pattern_references = [n.get("pattern") for n in nodes.values()]
    pattern_references.extend(d.get("pattern") for ident in junctions for d in nodes[ident].get("demands", []))
    pattern_references.extend(p.get("speed_pattern") for p in net.get("pumps", []))
    pattern_references.append(options.get("default_pattern"))
    for pattern in pattern_references:
        if pattern is not None and pattern not in patterns:
            raise CivilError("pattern_not_found", element_id=pattern)
    links, ids = [], set()
    for collection in ("pipes", "pumps", "valves"):
        for link in net.get(collection, []):
            ident = link.get("id")
            if not isinstance(ident, str) or not ident or ident in ids:
                raise CivilError("invalid_link_id", element_id=ident)
            ids.add(ident)
            if link.get("from") not in nodes or link.get("to") not in nodes:
                raise CivilError("node_not_found", element_id=ident)
            if link["from"] == link["to"]:
                raise CivilError("self_loop", element_id=ident)
            link["_kind"] = collection
            status = str(link.get("status", "active" if collection == "valves" else "open")).lower()
            if (status not in ("open", "closed", "cv", "active")
                    or (status == "cv" and collection != "pipes")
                    or (status == "active" and collection != "valves")):
                raise CivilError("invalid_link_status", element_id=ident)
            link["status"] = status
            if collection == "pumps":
                link["_fit"] = _pump_fit(link.get("curve"))
                link["speed"] = _number(link.get("speed", 1), "invalid_pump_speed", minimum=0)
            else:
                link["diameter"] = _number(link.get("diameter"), "invalid_pipe" if collection == "pipes" else "invalid_valve", positive=True)
                link["minor_loss"] = _number(link.get("minor_loss", 0), "invalid_minor_loss", minimum=0)
                if collection == "pipes":
                    link["length"] = _number(link.get("length"), "invalid_pipe", positive=True)
                    link["roughness"] = _number(link.get("roughness"), "invalid_roughness", positive=formula == "H-W", minimum=0)
                else:
                    link["type"] = str(link.get("type", "")).upper()
                    if link["type"] not in ("PRV", "PSV", "FCV", "TCV"):
                        raise CivilError("unsupported_valve", element_id=ident)
                    link["setting"] = _number(link.get("setting"), "invalid_valve_setting", minimum=0)
                    controlled = link["to"] if link["type"] == "PRV" else link["from"]
                    if link["type"] in ("PRV", "PSV") and controlled not in junctions:
                        raise CivilError("invalid_valve_connection", "Controlled pressure node must be a junction.", element_id=ident)
            links.append(link)
    if not links:
        raise CivilError("invalid_network", "A water network requires at least one pipe, pump, or valve.")
    for control in net.get("controls", []):
        if control.get("link_id") not in ids or control.get("status") not in ("open", "closed"):
            raise CivilError("invalid_control")
        if "time_s" in control:
            control["time_s"] = _number(control["time_s"], "invalid_control", minimum=0)
        elif control.get("node_id") in tanks and control.get("relation") in ("below", "above"):
            control["level_m"] = _number(control.get("level_m"), "invalid_control")
        else:
            raise CivilError("unsupported_control", "Only tank-level and elapsed-time OPEN/CLOSED controls are supported.")
    return net, options, formula, nodes, junctions, fixed, tanks, links


def _pattern(net, options, ident, t):
    if ident is None:
        return 1.0
    values = net["patterns"][ident]
    slot = math.floor((t+options.get("pattern_start_s", 0)+1e-9)/options["pattern_step_s"])
    return values[slot % len(values)]


def _demands(net, options, nodes, junctions, t):
    out = np.zeros(len(junctions))
    for i, ident in enumerate(junctions):
        n = nodes[ident]
        terms = [{"demand_m3s": n["demand_m3s"], "pattern": n.get("pattern")}]+n.get("demands", [])
        out[i] = sum(d["demand_m3s"]*_pattern(net, options, d.get("pattern") or options.get("default_pattern"), t) for d in terms)*options.get("demand_multiplier", 1)
    return out


def _delivered(options, demand, pressure):
    delivered, derivative = demand.copy(), np.zeros(len(demand))
    if options["demand_model"] == "PDA":
        pmin, preq, exponent = options["p_min_m"], options["p_req_m"], options["pda_exponent"]
        positive = demand > 0
        below = positive & (pressure <= pmin)
        between = positive & (pressure > pmin) & (pressure < preq)
        delivered[below] = 0
        fraction = (pressure[between]-pmin)/(preq-pmin)
        delivered[between] = demand[between]*fraction**exponent
        derivative[between] = demand[between]*exponent*fraction**(exponent-1)/(preq-pmin)
    return delivered, derivative


def _connected(nodes, junctions, fixed, links, statuses):
    adjacency = {ident: [] for ident in nodes}
    for k, link in enumerate(links):
        if statuses[k] in ("closed", "tank_closed", "head_closed"):
            continue
        adjacency[link["from"]].append(link["to"])
        adjacency[link["to"]].append(link["from"])
    reached, stack = set(fixed), list(fixed)
    while stack:
        for neighbor in adjacency[stack.pop()]:
            if neighbor not in reached:
                reached.add(neighbor)
                stack.append(neighbor)
    missing = sorted(set(junctions)-reached)
    if missing:
        raise CivilError("disconnected_nodes", "Junctions have no open hydraulic path to a head boundary.", ids=missing)


def _minor(link, q, *, throttle_active=True):
    k = link.get("minor_loss", 0)
    if link.get("type") == "TCV" and throttle_active:
        k += link["setting"]
    area = math.pi*link["diameter"]**2/4
    coefficient = k/(2*_G*area**2)
    return coefficient*q*abs(q), max(2*coefficient*abs(q), _GMIN)


def _status_updates(links, statuses, imposed, q, heads, nodes, levels, speeds):
    """EPANET v2.2 hydstatus.c valve/CV/pump logic with exact closed flow."""
    changed = False
    for k, link in enumerate(links):
        ident, kind = link["id"], link["_kind"]
        old = statuses[k]
        specified = imposed.get(ident, link["status"])
        if specified == "closed" or (kind == "pumps" and speeds[k] == 0):
            new = "closed"
        else:
            h1, h2 = heads[link["from"]], heads[link["to"]]
            dh = h1-h2
            new = "open" if old in ("tank_closed", "head_closed") else old
            if kind == "pumps":
                shutoff = _pump_loss(link["_fit"], 0, speeds[k])[2]
                new = "head_closed" if -dh > shutoff+_HTOL or q[k] < -_QTOL else "open"
            elif specified == "cv":
                if abs(dh) > _HTOL:
                    new = "closed" if dh < -_HTOL or q[k] < -_QTOL else "open"
                elif q[k] < -_QTOL:
                    new = "closed"
            elif kind == "valves" and specified not in ("open", "closed"):
                valve = link["type"]
                hml = link.get("minor_loss", 0)*q[k]**2/(2*_G*(math.pi*link["diameter"]**2/4)**2)
                if valve in ("PRV", "PSV"):
                    controlled = link["to"] if valve == "PRV" else link["from"]
                    target = nodes[controlled]["elevation"]+link["setting"]
                    if old == "active":
                        if q[k] < -_QTOL:
                            new = "closed"
                        elif (valve == "PRV" and h1-hml < target-_HTOL) or (valve == "PSV" and h2+hml > target+_HTOL):
                            new = "open"
                    elif old == "open":
                        if q[k] < -_QTOL:
                            new = "closed"
                        elif (valve == "PRV" and h2 >= target+_HTOL) or (valve == "PSV" and h1 < target-_HTOL):
                            new = "active"
                    else:
                        if valve == "PRV":
                            if h1 >= target+_HTOL and h2 < target-_HTOL:
                                new = "active"
                            elif h1 < target-_HTOL and h1 > h2+_HTOL:
                                new = "open"
                        elif h2 > target+_HTOL and h1 > h2+_HTOL:
                            new = "open"
                        elif h1 >= target+_HTOL and h1 > h2+_HTOL:
                            new = "active"
                elif valve == "FCV":
                    if dh < -_HTOL or q[k] < -_QTOL or (old == "active" and dh < hml-_HTOL):
                        new = "open"
                    elif old == "open" and q[k] >= link["setting"]+_QTOL:
                        new = "active"
            # Bound tank links close only in the forbidden direction. A closed
            # link can reopen on a head reversal without disturbing other links.
            potential = dh
            if kind == "pumps":
                potential += _pump_loss(link["_fit"], 0, speeds[k])[2]
            for endpoint, direction in ((link["from"], 1), (link["to"], -1)):
                if endpoint not in levels or new in ("closed", "head_closed"):
                    continue
                tank = nodes[endpoint]
                outward = direction*(q[k] if old != "tank_closed" else potential)
                if (levels[endpoint] >= tank["max_level"]-1e-9 and outward < -1e-12) or (levels[endpoint] <= tank["min_level"]+1e-9 and outward > 1e-12):
                    new = "tank_closed"
        if new != old:
            statuses[k] = new
            changed = True
    return changed


def _steady(net, options, formula, nodes, junctions, fixed, tanks, links, levels, imposed, t, initial=None):
    n, m = len(junctions), len(links)
    index = {ident: i for i, ident in enumerate(junctions)}
    boundary = {ident: nodes[ident]["head"]*_pattern(net, options, nodes[ident].get("pattern"), t)
                for ident in fixed if ident not in tanks}
    boundary.update({ident: nodes[ident]["elevation"]+level for ident, level in levels.items()})
    elevation = np.array([nodes[ident]["elevation"] for ident in junctions])
    required = _demands(net, options, nodes, junctions, t)
    fixed_demand = np.array([nodes[ident].get("_fixed_demand_m3s", 0) for ident in junctions])
    speeds = np.array([link.get("speed", 1)*_pattern(net, options, link.get("speed_pattern"), t) for link in links])
    rows, cols, entries, boundary_difference = [], [], [], np.zeros(m)
    for k, link in enumerate(links):
        for ident, sign in ((link["from"], 1), (link["to"], -1)):
            if ident in index:
                rows.append(index[ident]); cols.append(k); entries.append(sign)
            else:
                boundary_difference[k] += sign*boundary[ident]
    B = coo_matrix((entries, (rows, cols)), shape=(n, m)).tocsr()
    statuses = [imposed.get(l["id"], l["status"]) for l in links]
    statuses = ["open" if s == "cv" else s for s in statuses]
    for k, link in enumerate(links):
        if link["_kind"] == "pumps" and speeds[k] == 0:
            statuses[k] = "closed"
    _connected(nodes, junctions, fixed, links, statuses)
    if initial:
        H = np.array([initial["nodes"][ident]["head_m"] for ident in junctions])
        q = np.array([initial["links"][l["id"]]["flow_m3s"] for l in links])
    else:
        H = np.full(n, sum(boundary.values())/len(boundary))
        q = np.full(m, max(float(np.sum(abs(required)))/max(m, 1), 0.001))
    converged, relative, energy_max, continuity_max = False, math.inf, math.inf, math.inf

    def evaluate(head, flow, nonlinear, pressure_valves):
        demand, slope = _delivered(options, required, head-elevation)
        continuity = B@flow+demand+fixed_demand
        head_difference = B.T@head+boundary_difference
        loss, gradient = np.zeros(len(nonlinear)), np.zeros(len(nonlinear))
        for j, k in enumerate(nonlinear):
            link = links[k]
            if link["_kind"] == "pipes":
                loss[j], gradient[j] = _pipe_loss(link, flow[k], formula, options["viscosity_m2s"])
            elif link["_kind"] == "pumps":
                loss[j], gradient[j], _ = _pump_loss(link["_fit"], flow[k], speeds[k])
            else:
                loss[j], gradient[j] = _minor(link, flow[k], throttle_active=statuses[k] == "active")
        residual = loss-head_difference[nonlinear]
        constraint = np.array([head[index[links[k]["to"] if links[k]["type"] == "PRV" else links[k]["from"]]]
                               -nodes[links[k]["to"] if links[k]["type"] == "PRV" else links[k]["from"]]["elevation"]-links[k]["setting"] for k in pressure_valves])
        return continuity, residual, constraint, gradient, slope, demand

    for iteration in range(1, options["max_iter"]+1):
        closed = [k for k, s in enumerate(statuses) if s in ("closed", "tank_closed", "head_closed")]
        fcv = [k for k, s in enumerate(statuses) if s == "active" and links[k].get("type") == "FCV"]
        pressure_valves = [k for k, s in enumerate(statuses) if s == "active" and links[k].get("type") in ("PRV", "PSV")]
        nonlinear = [k for k in range(m) if k not in closed and k not in fcv and k not in pressure_valves]
        q[closed] = 0
        for k in fcv:
            q[k] = links[k]["setting"]
        _connected(nodes, junctions, fixed, links, statuses)
        continuity, residual, constraint, gradient, demand_slope, _ = evaluate(H, q, nonlinear, pressure_valves)
        BN = B[:, nonlinear]
        inverse = 1/gradient
        A = BN@diags(inverse)@BN.T+diags(demand_slope)
        rhs = BN@(inverse*residual)-continuity
        if pressure_valves:
            controlled_indices = [index[links[k]["to"] if links[k]["type"] == "PRV" else links[k]["from"]] for k in pressure_valves]
            E = coo_matrix((np.ones(len(pressure_valves)), (np.arange(len(pressure_valves)), controlled_indices)), shape=(len(pressure_valves), n)).tocsr()
            matrix = bmat([[A, B[:, pressure_valves]], [E, None]], format="csr")
            rhs = np.concatenate((rhs, -constraint))
        else:
            matrix = A.tocsr()
        if len(rhs):
            try:
                with _warnings.catch_warnings():
                    _warnings.simplefilter("error", MatrixRankWarning)
                    correction = spsolve(matrix, rhs)
            except (MatrixRankWarning, RuntimeError, ValueError) as exc:
                raise CivilError("singular_hydraulics", "Head constraints or fixed-flow devices leave an undetermined hydraulic component.") from exc
            if not np.all(np.isfinite(correction)):
                raise CivilError("singular_hydraulics")
        else:
            correction = np.zeros(0)
        dH = correction[:n]
        dq = np.zeros(m)
        dq[nonlinear] = inverse*(BN.T@dH-residual)
        if pressure_valves:
            dq[pressure_valves] = correction[n:]
        # Backtracking makes direct Wagner nodal derivatives robust near p_min
        # without substituting demand or forcing positive nodal pressure.
        norm = max(float(np.max(abs(continuity), initial=0))/max(float(np.sum(abs(required)+abs(fixed_demand))), 0.001),
                   float(np.max(abs(residual), initial=0)), float(np.max(abs(constraint), initial=0)))
        factor = 1.0
        for _ in range(24):
            trial = evaluate(H+factor*dH, q+factor*dq, nonlinear, pressure_valves)
            trial_norm = max(float(np.max(abs(trial[0]), initial=0))/max(float(np.sum(abs(required)+abs(fixed_demand))), 0.001),
                             float(np.max(abs(trial[1]), initial=0)), float(np.max(abs(trial[2]), initial=0)))
            if trial_norm <= norm*(1-1e-4*factor) or trial_norm < 1e-9:
                break
            factor *= 0.5
        H += factor*dH
        q += factor*dq
        relative = float(np.sum(abs(factor*dq)))/max(float(np.sum(abs(q))), 1e-12)
        continuity, residual, constraint, _, _, _ = evaluate(H, q, nonlinear, pressure_valves)
        energy_max = max(float(np.max(abs(residual), initial=0)), float(np.max(abs(constraint), initial=0)))
        continuity_max = float(np.max(abs(continuity), initial=0))
        heads = dict(boundary)
        heads.update({ident: float(H[i]) for i, ident in enumerate(junctions)})
        # Change statuses only after a consistent hydraulic iteration. Early CV
        # reversal from an arbitrary initialization must not disconnect a network.
        stable = True
        if (energy_max < 1e-5 and continuity_max < 1e-9) or iteration % 10 == 0:
            stable = not _status_updates(links, statuses, imposed, q, heads, nodes, levels, speeds)
        if stable and relative <= options["accuracy"] and energy_max <= 1e-6 and continuity_max <= 1e-10:
            converged = True
            break
    demand, _ = _delivered(options, required, H-elevation)
    output_nodes = {ident: {"head_m": float(H[i]), "pressure_m": float(H[i]-elevation[i]),
                            "demand_m3s": float(required[i]+fixed_demand[i]),
                            "demand_delivered_m3s": float(demand[i]+fixed_demand[i])} for i, ident in enumerate(junctions)}
    tank_net = {ident: 0.0 for ident in tanks}
    for ident in fixed:
        output_nodes[ident] = {"head_m": float(boundary[ident]),
                               "pressure_m": float(levels[ident]) if ident in tanks else 0.0,
                               "demand_m3s": 0.0, "demand_delivered_m3s": 0.0}
    output_links = {}
    for k, link in enumerate(links):
        if statuses[k] in ("closed", "tank_closed", "head_closed"):
            q[k] = 0
        area = math.pi*link["diameter"]**2/4 if "diameter" in link else None
        output_links[link["id"]] = {"flow_m3s": float(q[k]), "velocity_ms": float(abs(q[k])/area) if area else 0.0,
                                   "headloss_m": float(heads[link["from"]]-heads[link["to"]]), "status": statuses[k]}
        if link["from"] in tank_net:
            tank_net[link["from"]] -= float(q[k])
        if link["to"] in tank_net:
            tank_net[link["to"]] += float(q[k])
    # Recompute continuity after a last-iteration status change. Report residuals
    # from the returned state, not from flows subsequently forced to zero.
    returned_continuity = B@q+demand+fixed_demand
    continuity_max = float(np.max(abs(returned_continuity), initial=0))
    timestep = {"t_s": float(t), "nodes": output_nodes, "links": output_links,
                "tanks": {ident: {"level_m": float(level)} for ident, level in levels.items()},
                "converged": converged, "iterations": iteration,
                "relative_flow_change": relative, "max_energy_residual_m": energy_max,
                "max_continuity_residual_m3s": continuity_max,
                "max_residual": max(energy_max, continuity_max)}
    return timestep, tank_net


def _controls(net, levels, t, imposed):
    # Time controls are one-shot events, not perpetual predicates: an old OPEN
    # must not overwrite a later level-triggered CLOSED on every hydraulic step.
    def apply(control):
        imposed[control["link_id"]] = control["status"]
        if control["status"] == "open":
            # EPANET changestatus: pump OPEN resets relative speed to 1.
            for pump in net.get("pumps", []):
                if pump["id"] == control["link_id"]:
                    pump["speed"] = 1.0
    previous = net.get("_last_control_time_s", -math.inf)
    time_controls = sorted((c for c in net.get("controls", []) if "time_s" in c), key=lambda c: c["time_s"])
    for control in time_controls:
        if previous < control["time_s"] <= t+1e-8:
            apply(control)
    net["_last_control_time_s"] = t
    for control in (c for c in net.get("controls", []) if "time_s" not in c):
        trigger = (levels[control["node_id"]] <= control["level_m"]+1e-8 if control["relation"] == "below" else
                   levels[control["node_id"]] >= control["level_m"]-1e-8)
        if trigger:
            apply(control)


def _checks(timestep, nodes, junctions, limits):
    rows = []
    for ident in junctions:
        pressure = timestep["nodes"][ident]["pressure_m"]
        for key, passed, check in (("water_min_pressure_m", pressure >= limits["water_min_pressure_m"], "minimum_pressure"),
                                   ("water_max_pressure_m", pressure <= limits["water_max_pressure_m"], "maximum_pressure")):
            rows.append({"check": check, "element_id": ident, "t_s": timestep["t_s"], "value": pressure,
                         "limit": limits[key], "units": "m", "pass": passed, "criterion": key,
                         "reference": limits["references"][key]})
    for ident, link in timestep["links"].items():
        rows.append({"check": "maximum_velocity", "element_id": ident, "t_s": timestep["t_s"],
                     "value": link["velocity_ms"], "limit": limits["water_max_velocity_ms"], "units": "m/s",
                     "pass": link["velocity_ms"] <= limits["water_max_velocity_ms"], "criterion": "water_max_velocity_ms",
                     "reference": limits["references"]["water_max_velocity_ms"]})
    return rows


def solve(network: dict, *, criteria: dict | None = None) -> dict:
    """Solve steady/EPS DDA or Wagner PDA using sparse global gradient equations.

    See EPANET 2.2 §12.1. Junction demand can be negative (prescribed inflow);
    PDA scales only positive base demand, never prescribed negative injection.
    EPS returns all hydraulic/event instants including the final duration.
    """
    from .criteria import resolve
    limits = resolve(criteria)
    net, options, formula, nodes, junctions, fixed, tanks, links = _prepare(network)
    levels = {ident: tank["init_level"] for ident, tank in tanks.items()}
    imposed, timesteps, checks, warn = {}, [], [], []
    t, duration, initial = 0.0, options["duration_s"], None
    while True:
        _controls(net, levels, t, imposed)
        timestep, tank_net = _steady(net, options, formula, nodes, junctions, fixed, tanks, links, levels, imposed, t, initial)
        timesteps.append(timestep)
        checks.extend(_checks(timestep, nodes, junctions, limits))
        for pump in net.get("pumps", []):
            state = timestep["links"][pump["id"]]
            if state["status"] == "open" and state["headloss_m"] > 1e-6:
                warn.append(f"Pump {pump['id']} operates beyond its zero-head capacity at t={t:g} s; curve extrapolation has negative head gain.")
        if not timestep["converged"]:
            warn.append(f"Hydraulics did not converge at t={t:g} s; EPS advancement stopped.")
            break
        if t >= duration-1e-8:
            break
        next_hyd = (math.floor((t+1e-8)/options["hydraulic_step_s"])+1)*options["hydraulic_step_s"]
        shifted = t+options.get("pattern_start_s", 0)
        next_pattern = (math.floor((shifted+1e-8)/options["pattern_step_s"])+1)*options["pattern_step_s"]-options.get("pattern_start_s", 0)
        dt = min(duration-t, next_hyd-t, next_pattern-t)
        rates = {ident: tank_net[ident]/(math.pi*tank["diameter"]**2/4) for ident, tank in tanks.items()}
        for ident, rate in rates.items():
            if abs(rate) > 1e-15:
                boundary = tanks[ident]["max_level"] if rate > 0 else tanks[ident]["min_level"]
                event = (boundary-levels[ident])/rate
                if event > 1e-8:
                    dt = min(dt, event)
        for control in net.get("controls", []):
            if "time_s" in control:
                event = control["time_s"]-t
            else:
                rate = rates[control["node_id"]]
                event = (control["level_m"]-levels[control["node_id"]])/rate if abs(rate) > 1e-15 else math.inf
            if event > 1e-8:
                dt = min(dt, event)
        if dt <= 1e-8:
            raise CivilError("zero_hydraulic_timestep", "Coincident events could not advance the EPS clock.")
        for ident, rate in rates.items():
            levels[ident] += rate*dt
            if levels[ident] < tanks[ident]["min_level"]-1e-7 or levels[ident] > tanks[ident]["max_level"]+1e-7:
                raise CivilError("tank_volume_out_of_bounds", element_id=ident)
        t += dt
        initial = timestep
    for entry in net.get("epanet_passthrough", []):
        if entry.get("active"):
            warn.append(f"EPANET [{entry['section']}] preserved but not analyzed (non-hydraulic section).")
    assumptions = ["SI metres and m3/s; Darcy/minor-loss gravity 9.80665 m/s2.",
                   "Hazen–Williams uses EPANET 4.727 US coefficient converted to SI (10.66682948893); low-flow linear gradient.",
                   "Darcy–Weisbach: laminar 64/Re, EPANET Dunlop transition cubic, Swamee–Jain turbulent friction.",
                   "EPS cylindrical tank Euler volume update; fractional-second event localization (EPANET rounds event seconds).",
                   "PDA Wagner pressure law applies positive demands; signed negative demands remain prescribed inflows.",
                   "Default omitted hydraulic options are the aicad.civil/1 contract values.",
                   "Pump multipoint segments extrapolate linearly at curve ends, as in EPANET; one/three-point curves use a power law.",
                   "max_residual is max of energy residual normalized by 1 m and continuity residual normalized by 1 m3/s."]
    return {"ok": True, "converged": all(s["converged"] for s in timesteps),
            "iterations": sum(s["iterations"] for s in timesteps),
            "max_residual": max(s["max_residual"] for s in timesteps), "timesteps": timesteps,
            "checks": checks, "warnings": warn, "assumptions": assumptions, "method": dict(_METHOD)}


def fire_flow(network: dict, node_id: str, flow_m3s: float | None = None, *,
              residual_min_m: float | None = None, criteria: dict | None = None) -> dict:
    """Fixed withdrawal or bisection at the minimum pressure of ALL junctions.

    EPANET 2.2 fire-flow scenario semantics with Wagner base-demand model
    retained; fire demand itself is prescribed, not pressure-scaled. Scenario is
    at t=0 and initial tank levels (not a duration/supply-volume endurance test).
    """
    from .criteria import resolve
    limits = resolve(criteria)
    residual = _number(limits["fire_residual_min_m"] if residual_min_m is None else residual_min_m,
                       "invalid_residual_pressure", minimum=0)
    scenario = deepcopy(network)
    target = next((n for n in scenario.get("junctions") or [] if n.get("id") == node_id), None)
    if target is None:
        raise CivilError("not_found", "Fire-flow node must be a junction.", element_id=node_id)
    if scenario.get("options") is None:
        scenario["options"] = {}
    scenario["options"]["duration_s"] = 0
    base_fixed = target.get("_fixed_demand_m3s", 0)

    def run(flow):
        target["_fixed_demand_m3s"] = base_fixed+flow
        result = solve(scenario, criteria=criteria)
        if not result["converged"]:
            raise CivilError("fire_flow_not_converged", flow_m3s=flow, max_residual=result["max_residual"])
        nodes = result["timesteps"][0]["nodes"]
        pressures = {j["id"]: nodes[j["id"]]["pressure_m"] for j in scenario["junctions"]}
        limiting = min(pressures, key=pressures.get)
        return result, pressures, limiting, pressures[limiting]

    warnings = []
    if flow_m3s is not None:
        flow = _number(flow_m3s, "invalid_fire_flow", minimum=0)
        result, pressures, limiting, minimum = run(flow)
        iterations = 0
    else:
        result, pressures, limiting, minimum = run(0)
        flow, iterations = 0.0, 0
        if minimum < residual:
            warnings.append("Baseline pressure is already below the required fire residual; no additional flow is available.")
        else:
            lower = 0.0
            upper = max(sum(max(0, j.get("demand_m3s", 0)) for j in scenario["junctions"]), 0.01)
            for _ in range(50):
                trial = run(upper)
                if trial[3] < residual:
                    break
                lower = upper
                upper *= 2
            else:
                raise CivilError("unbounded_fire_flow", "No finite pressure-limited bracket exists in this model.")
            for iterations in range(1, 81):
                middle = (lower+upper)/2
                trial = run(middle)
                if trial[3] >= residual:
                    lower = middle
                else:
                    upper = middle
                # An infeasible midpoint close to the pressure target is NOT
                # the returned feasible lower endpoint. Refine the entire flow
                # bracket so that returning lower cannot lose root accuracy.
                if upper-lower <= 1e-10:
                    break
            flow = lower
            result, pressures, limiting, minimum = run(flow)
    return {"ok": True, "converged": result["converged"], "node_id": node_id,
            "flow_m3s": float(flow), "available_flow_m3s": float(flow) if flow_m3s is None else None,
            "residual_min_m": residual, "minimum_pressure_m": float(minimum),
            "residual_pressures_m": pressures, "limiting_node_id": limiting,
            "pass": minimum >= residual-1e-7, "iterations": iterations,
            "hydraulics": result, "warnings": warnings+result["warnings"],
            "assumptions": result["assumptions"]+["Fire withdrawal is fixed; base demand model retained; initial operating state only."],
            "method": {"name": "System-wide residual-pressure fire-flow scenario and bracketed bisection",
                       "reference": "EPANET 2.2 User Manual hydraulic scenario analysis; Wagner et al. (1988) PDA"}}
