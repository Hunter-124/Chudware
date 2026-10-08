"""Steady gravity conveyance and preliminary drainage design, in SI units.

Manning and section geometry: FHWA HEC-22, 4th ed. (2024), chapters 6/9;
critical flow, standard-step energy equation and rectangular hydraulic jumps:
Chow, *Open-Channel Hydraulics* (1959). Network HGL/EGL is an explicitly
preliminary steady screening calculation, NOT SWMM dynamic-wave routing and
NOT the FHWA access-hole energy-loss procedure of HEC-22 §9.1.6.7.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
from typing import Callable

from scipy.optimize import brentq

from .errors import CivilError

G = 9.80665
_REFERENCE = "FHWA HEC-22, 4th ed. (2024), chapters 6 and 9; Chow (1959)"
_THETA_MAX = brentq(lambda t: 5 * t * (1 - math.cos(t)) - 2 * (t - math.sin(t)), math.pi, 2 * math.pi)
_MAX_RATIO = (1 - math.cos(_THETA_MAX / 2)) / 2
_UNSET = object()


def _result(name: str, **values) -> dict:
    return {"ok": True, **values, "warnings": [], "assumptions": [],
            "method": {"name": name, "reference": _REFERENCE}}


def _number(value, name: str, *, positive=False, nonnegative=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CivilError("invalid_input", field=name, value=value)
    value = float(value)
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise CivilError("invalid_input", field=name, value=value)
    return value


def _shape(spec: dict) -> tuple[str, float, float | None, float, bool]:
    if not isinstance(spec, dict):
        raise CivilError("invalid_shape")
    kind = spec.get("shape")
    if kind == "circular":
        d = _number(spec.get("diameter"), "diameter", positive=True)
        return kind, d, d, 0.0, True
    if kind not in ("rectangular", "trapezoidal"):
        raise CivilError("unsupported_shape", shape=kind)
    width = _number(spec.get("width"), "width", nonnegative=kind == "trapezoidal", positive=kind == "rectangular")
    height = spec.get("height")
    if height is not None:
        height = _number(height, "height", positive=True)
    side = _number(spec.get("side_slope"), "side_slope", nonnegative=True) if kind == "trapezoidal" else 0.0
    if kind == "trapezoidal" and width == 0 and side == 0:
        raise CivilError("invalid_shape", "A trapezoid needs a positive bottom width or side slope.")
    closed = spec.get("closed", False)
    if not isinstance(closed, bool) or (closed and (kind != "rectangular" or height is None)):
        raise CivilError("invalid_shape", "Only a finite-height rectangular section can be closed.")
    return kind, width, height, side, closed


def _geometry(spec: dict, depth: float) -> tuple[float, float, float, float]:
    kind, width, height, side, closed = _shape(spec)
    y = _number(depth, "depth_m", nonnegative=True)
    if height is not None and y > height:
        raise CivilError("depth_exceeds_section", depth_m=y, height_m=height)
    if y == 0:
        return 0.0, 0.0, 0.0, width if kind != "circular" else 0.0
    if kind == "circular":
        theta = 4 * math.asin(math.sqrt(y / width))
        # Prevent subtractive cancellation in theta - sin(theta) near the invert.
        diff = theta - math.sin(theta) if theta >= 0.02 else theta ** 3 * (1 / 6 - theta ** 2 / 120 + theta ** 4 / 5040)
        area = width ** 2 * diff / 8
        perimeter = width * theta / 2
        top = 0.0 if y == width else width * math.sin(theta / 2)
    elif kind == "rectangular":
        area, perimeter, top = width * y, width + 2 * y, width
        if closed and y == height:
            perimeter += width
            top = 0.0
    else:
        area = y * (width + side * y)
        perimeter = width + 2 * y * math.sqrt(1 + side * side)
        top = width + 2 * side * y
    return area, perimeter, area / perimeter, top


def partial_flow(shape_spec: dict, depth_m: float) -> dict:
    """Exact circular/rectangular/trapezoidal wetted geometry (HEC-22 ch. 6/9)."""
    a, p, r, t = _geometry(shape_spec, depth_m)
    return _result("exact wetted section geometry", area_m2=a, wetted_perimeter_m=p,
                   hydraulic_radius_m=r, top_width_m=t)


def _manning(spec, depth, slope, n) -> float:
    a, _, r, _ = _geometry(spec, depth)
    return a * r ** (2 / 3) * math.sqrt(slope) / n


def _flow_inputs(flow, slope=_UNSET, n=_UNSET):
    q = _number(flow, "flow_m3s", nonnegative=True)
    s = _number(slope, "slope", positive=True) if slope is not _UNSET else None
    roughness = _number(n, "n", positive=True) if n is not _UNSET else None
    return q, s, roughness


def _froude(spec, depth, flow) -> tuple[float | None, str]:
    a, _, _, t = _geometry(spec, depth)
    if a == 0:
        return (0.0, "dry") if flow == 0 else (None, "undefined")
    if t == 0:
        return None, "pressurized"
    fr = flow * math.sqrt(t / (G * a ** 3))
    return fr, "critical" if abs(fr - 1) < 1e-6 else ("subcritical" if fr < 1 else "supercritical")


def froude(shape_spec: dict, depth_m: float, flow_m3s: float) -> dict:
    """Section Froude number Q sqrt(T/(g A³)); undefined for pressurized flow."""
    q, _, _ = _flow_inputs(flow_m3s)
    fr, regime = _froude(shape_spec, depth_m, q)
    if regime == "undefined":
        raise CivilError("flow_in_dry_section")
    return _result("section Froude number", froude=fr, regime=regime)


def manning_flow(shape_spec: dict, depth_m: float, slope: float, n: float) -> dict:
    """SI Manning Q=A R^(2/3) sqrt(S)/n, without US customary coefficient."""
    _, s, roughness = _flow_inputs(0, slope, n)
    a, _, _, _ = _geometry(shape_spec, depth_m)
    q = _manning(shape_spec, depth_m, s, roughness)
    fr, regime = _froude(shape_spec, depth_m, q)
    return _result("Manning uniform flow", flow_m3s=q, depth_m=depth_m,
                   velocity_ms=q / a if a else 0.0, froude=fr, regime=regime)


def full_flow_capacity(shape_spec: dict, slope: float, n: float) -> dict:
    """Full/bankfull Manning capacity; circular maximum occurs at ~0.938D."""
    _, s, roughness = _flow_inputs(0, slope, n)
    kind, _, height, _, closed = _shape(shape_spec)
    if height is None:
        raise CivilError("missing_height", "A full/bankfull capacity requires a section height.")
    q = _manning(shape_spec, height, s, roughness)
    area = _geometry(shape_spec, height)[0]
    max_depth = height * _MAX_RATIO if kind == "circular" else height
    max_q = _manning(shape_spec, max_depth, s, roughness)
    # The roof is wetted only in full closed rectangular flow; free-surface
    # limiting capacity just below the roof is larger than pressure-full Q.
    if kind == "rectangular" and closed:
        open_spec = {**shape_spec, "closed": False}
        max_q = _manning(open_spec, height, s, roughness)
    return _result("Manning full and maximum capacity", flow_m3s=q, full_flow_m3s=q,
                   velocity_ms=q / area, max_flow_m3s=max_q, max_depth_m=max_depth)


def normal_depth(shape_spec: dict, flow_m3s: float, slope: float, n: float) -> dict:
    """Bracketed Manning inverse; returns the shallower circular root.

    Circular flows above pressure-full Q but below maximum Q have two roots;
    the ascending-capacity (shallower) branch is chosen and reported explicitly.
    """
    q, s, roughness = _flow_inputs(flow_m3s, slope, n)
    kind, width, height, _, closed = _shape(shape_spec)
    full = None
    solve_shape = shape_spec
    if height is not None:
        capacity = full_flow_capacity(shape_spec, s, roughness)
        full = capacity["flow_m3s"]
        upper = capacity["max_depth_m"]
        if q > capacity["max_flow_m3s"]:
            raise CivilError("exceeds_capacity", flow_m3s=q, max_flow_m3s=capacity["max_flow_m3s"], full_flow_m3s=full)
        if kind == "rectangular" and closed:
            solve_shape = {**shape_spec, "closed": False}
    else:
        upper = max(1.0, width)
        for _ in range(100):
            if _manning(shape_spec, upper, s, roughness) >= q:
                break
            upper *= 2
        else:
            raise CivilError("not_converged", "Could not bracket Manning normal depth.")
    if q == 0:
        y = 0.0
    elif q == _manning(solve_shape, upper, s, roughness):
        y = upper
    else:
        y = brentq(lambda d: _manning(solve_shape, d, s, roughness) - q, 0, upper, xtol=1e-12)
    a = _geometry(solve_shape, y)[0]
    fr, regime = _froude(solve_shape, y, q)
    out = _result("bracketed Manning normal depth", depth_m=y, flow_m3s=q,
                  full_flow_m3s=full, velocity_ms=q / a if a else 0.0,
                  froude=fr, regime=regime, converged=True)
    if kind == "circular":
        out["assumptions"].append("Ascending-capacity, shallower circular normal-depth branch.")
    return out


def critical_depth(shape_spec: dict, flow_m3s: float) -> dict:
    """Solve Q²T/(g A³)=1; rectangular result equals (q²/g)^(1/3)."""
    q, _, _ = _flow_inputs(flow_m3s)
    kind, width, height, _, closed = _shape(shape_spec)
    if q == 0:
        return _result("critical specific energy", depth_m=0.0, flow_m3s=0.0)
    if kind == "rectangular":
        y = (q * q / (G * width * width)) ** (1 / 3)
        if height is not None and (y > height or (closed and y >= height)):
            raise CivilError("critical_depth_exceeds_section", depth_m=y, height_m=height)
    else:
        # A³-Q²T/g is finite at y=0 (also zero for a triangular section), so
        # bracket strictly above zero rather than returning its spurious root.
        low = (height or max(width, 1.0)) * 1e-10
        upper = height if height is not None else max(width, 1.0)
        def residual(y):
            a, _, _, t = _geometry(shape_spec, y)
            return a ** 3 - q * q * t / G
        if height is None:
            for _ in range(100):
                if residual(upper) >= 0:
                    break
                upper *= 2
            else:
                raise CivilError("not_converged", "Could not bracket critical depth.")
        elif residual(upper) < 0:
            raise CivilError("critical_depth_exceeds_section", height_m=height)
        if residual(low) >= 0:
            # For extremely small flows use a smaller positive bracket.
            low = min(low, math.sqrt(q) * 1e-8)
        y = brentq(residual, low, upper, xtol=1e-13)
    return _result("critical specific energy", depth_m=y, flow_m3s=q)


def _network(network: dict):
    if not isinstance(network, dict) or any(network.get(key) is not None and not isinstance(network[key], list) for key in ("nodes", "conduits")):
        raise CivilError("invalid_network")
    nodes, pipes = {}, {}
    for collection, target in ((network.get("nodes") or [], nodes), (network.get("conduits") or [], pipes)):
        for element in collection:
            if not isinstance(element, dict) or not isinstance(element.get("id"), str) or not element["id"] or element["id"] in target:
                raise CivilError("invalid_element_id")
            target[element["id"]] = element
    if not nodes:
        raise CivilError("invalid_network", "A gravity network requires at least one node.")
    incoming, outgoing = {k: [] for k in nodes}, {k: [] for k in nodes}
    for key, pipe in pipes.items():
        if pipe.get("from") not in nodes or pipe.get("to") not in nodes:
            raise CivilError("not_found", hint="Conduit endpoints must reference gravity nodes.", element_id=key)
        outgoing[pipe["from"]].append(key)
        incoming[pipe["to"]].append(key)
    degree = {k: len(v) for k, v in incoming.items()}
    queue = deque(k for k in nodes if degree[k] == 0)
    order = []
    while queue:
        key = queue.popleft()
        order.append(key)
        for pid in outgoing[key]:
            end = pipes[pid]["to"]
            degree[end] -= 1
            if degree[end] == 0:
                queue.append(end)
    if len(order) != len(nodes):
        raise CivilError("cycle_in_gravity_network")
    for key, links in outgoing.items():
        if len(links) > 1:
            raise CivilError("unsupported_flow_split", "Explicit hydraulic splitter routing is not implemented.", node_id=key)
    return nodes, pipes, incoming, outgoing, order


def _accumulate(nodes, pipes, incoming, outgoing, order, flows):
    added = {key: 0.0 for key in nodes}
    if flows is not None and not isinstance(flows, dict):
        raise CivilError("invalid_flows")
    for key, value in (flows or {}).items():
        q = _number(value, "flows." + str(key), nonnegative=True)
        if key in nodes and key in pipes:
            raise CivilError("ambiguous_flow_id", element_id=key)
        if key in nodes:
            added[key] += q
        elif key in pipes:
            added[pipes[key]["from"]] += q
        else:
            raise CivilError("not_found", element_id=key)
    node_flows, pipe_flows = {}, {}
    for key in order:
        q = _number(nodes[key].get("inflow_m3s", 0), "inflow_m3s", nonnegative=True) + added[key]
        q += sum(pipe_flows[pid] for pid in incoming[key])
        node_flows[key] = q
        for pid in outgoing[key]:
            pipe_flows[pid] = q
    return node_flows, pipe_flows


def _reach(pipe, nodes):
    start, end = nodes[pipe["from"]], nodes[pipe["to"]]
    z1 = pipe.get("inlet_invert")
    z2 = pipe.get("outlet_invert")
    z1 = _number(start.get("invert") if z1 is None else z1, "inlet_invert")
    z2 = _number(end.get("invert") if z2 is None else z2, "outlet_invert")
    length = pipe.get("length")
    if length is None:
        points = [[start.get("x"), start.get("y")], *pipe.get("vertices", []), [end.get("x"), end.get("y")]]
        for xy in points:
            if len(xy) != 2:
                raise CivilError("invalid_coordinates")
            _number(xy[0], "x")
            _number(xy[1], "y")
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
    length = _number(length, "length", positive=True)
    n = _number(pipe.get("n"), "n", positive=True)
    _shape(pipe)
    return z1, z2, length, (z1 - z2) / length, n


def _junction_k(node, incoming_pipe, outlet_pipe, nodes):
    """HEC-22 (2024) Table 9.4 preliminary K, or an explicit user K.

    Table angles are interior angles: 180° is straight. Nearest conservative
    table bin is used, without extrapolation; sharper-than-90° turns refuse.
    """
    if node.get("loss_k") is not None:
        return _number(node["loss_k"], "loss_k", nonnegative=True), "explicit node loss_k"
    if outlet_pipe is None:
        return 0.0, "no access-hole loss at outfall"
    xy = (node.get("x"), node.get("y"))
    a_node, b_node = nodes[incoming_pipe["from"]], nodes[outlet_pipe["to"]]
    a = incoming_pipe.get("vertices", [])[-1] if incoming_pipe.get("vertices") else [a_node.get("x"), a_node.get("y")]
    b = outlet_pipe.get("vertices", [])[0] if outlet_pipe.get("vertices") else [b_node.get("x"), b_node.get("y")]
    for value in (*xy, *a, *b):
        _number(value, "junction_coordinate")
    av, bv = (a[0] - xy[0], a[1] - xy[1]), (b[0] - xy[0], b[1] - xy[1])
    den = math.hypot(*av) * math.hypot(*bv)
    if den == 0:
        raise CivilError("junction_angle_undefined", "Supply loss_k when adjoining node coordinates coincide.", node_id=node["id"])
    cosine = (av[0] * bv[0] + av[1] * bv[1]) / den
    # Floating point round-off protection, not an input/engineering-value clamp.
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    if angle < 90 - 1e-8:
        raise CivilError("unsupported_junction_angle", "HEC-22 simplified table does not cover angles sharper than 90 degrees; supply loss_k.", node_id=node["id"], angle_deg=angle)
    if node.get("type") == "inlet":
        k = 0.5 if angle >= 180 - 1e-8 else 1.5
    else:
        k = 1.0
        for table_angle, coefficient in ((90, 1.0), (120, 0.85), (135, 0.75), (157.5, 0.45), (180, 0.15)):
            if angle >= table_angle - 1e-8:
                k = coefficient
    return k, "HEC-22 4th ed. §9.1.6.6 Table 9.4, conservative angle bin"


def analyze(network: dict, *, flows: dict | None = None, z_at: Callable | None = None, criteria: dict | None = None) -> dict:
    """Accumulate simultaneous peaks and screen steady reach HGL/EGL upstream.

    Free-surface reach losses use uniform normal-depth resistance. A reach with
    downstream water above its crown (or excessive design flow/adverse slope)
    uses full-section Manning friction. This is a preliminary envelope, not a
    gradually varied or transient network solver. Junction K uses outlet velocity
    head; no exit loss is assumed unless the outfall has explicit loss_k.
    """
    nodes, pipes, incoming, outgoing, order = _network(network)
    node_flows, pipe_flows = _accumulate(nodes, pipes, incoming, outgoing, order, flows)
    results = {}
    warnings = ["Preliminary HGL/EGL screening: HEC-22 Table 9.4 K is NOT the FHWA access-hole energy-loss method; §9.1.6.6 excludes this table from final EGL design."]
    for pid, pipe in pipes.items():
        z1, z2, length, slope, n = _reach(pipe, nodes)
        kind, _, height, _, closed = _shape(pipe)
        if height is None:
            raise CivilError("missing_height", element_id=pid)
        if not isinstance(pipe.get("allow_adverse_slope", False), bool):
            raise CivilError("invalid_input", field="allow_adverse_slope", element_id=pid)
        if slope <= 0 and not pipe.get("allow_adverse_slope", False):
            raise CivilError("adverse_slope", element_id=pid, slope=slope)
        q = pipe_flows[pid]
        capacity = full_flow_capacity(pipe, slope, n) if slope > 0 else None
        normal = None
        if slope > 0:
            try:
                normal = normal_depth(pipe, q, slope, n)
            except CivilError as error:
                if error.reason != "exceeds_capacity":
                    raise
                if not closed:
                    raise CivilError("open_channel_exceeds_capacity", "An open section cannot be replaced by pressure flow.", element_id=pid) from None
                warnings.append(f"{pid}: design flow exceeds maximum free-surface Manning capacity; full-section pressure-flow screening used.")
        else:
            warnings.append(f"{pid}: explicitly allowed adverse/zero slope; no uniform normal depth exists.")
        if normal is None and q > 0 and not closed:
            raise CivilError("unsupported_adverse_open_channel", element_id=pid)
        depth = normal["depth_m"] if normal else height
        if q == 0:
            depth = 0.0
        area = _geometry(pipe, depth)[0]
        velocity = q / area if area else 0.0
        try:
            critical = critical_depth(pipe, q)["depth_m"]
        except CivilError as error:
            if error.reason != "critical_depth_exceeds_section":
                raise
            if not closed:
                raise CivilError("open_channel_control_exceeds_section", element_id=pid) from None
            critical = height
            warnings.append(f"{pid}: critical depth exceeds section; crown used as pressure-flow boundary.")
        fr, regime = _froude(pipe, depth, q)
        results[pid] = {"flow_m3s": q, "length_m": length, "slope": slope, "inlet_invert_m": z1,
                        "outlet_invert_m": z2, "normal_depth_m": normal["depth_m"] if normal else None,
                        "critical_depth_m": critical, "depth_m": depth, "depth_ratio": depth / height,
                        "capacity_m3s": capacity["flow_m3s"] if capacity else None,
                        "max_capacity_m3s": capacity["max_flow_m3s"] if capacity else None,
                        "velocity_ms": velocity, "velocity_full_ms": capacity["velocity_ms"] if capacity else None,
                        "froude": fr, "regime": regime, "exceeds_capacity": normal is None and q > 0}
    node_results = {}
    for nid in reversed(order):
        node = nodes[nid]
        invert = _number(node.get("invert"), "invert")
        if not outgoing[nid]:
            tail = node.get("outfall_tailwater")
            tail = 0.0 if tail is None else _number(tail, "outfall_tailwater", nonnegative=True)
            controls = [max(results[p]["critical_depth_m"], results[p]["depth_m"]) + results[p]["outlet_invert_m"] for p in incoming[nid]]
            hgl = max([invert + tail, *controls])
            velocities = []
            for pid in incoming[nid]:
                reach, pipe = results[pid], pipes[pid]
                height = _shape(pipe)[2]
                depth = height if hgl >= reach["outlet_invert_m"] + height else max(reach["depth_m"], hgl - reach["outlet_invert_m"])
                area = _geometry(pipe, depth)[0]
                velocities.append(reach["flow_m3s"] / area if area else 0.0)
            vh = max([0.0, *(v * v / (2 * G) for v in velocities)])
            egl = hgl + vh
            if node.get("type") != "outfall":
                warnings.append(f"{nid}: terminal node treated as a free outfall.")
        else:
            pid = outgoing[nid][0]
            pipe, reach = pipes[pid], results[pid]
            downstream = node_results[pipe["to"]]
            downstream_outlet = pipes[outgoing[pipe["to"]][0]] if outgoing[pipe["to"]] else None
            k, k_source = _junction_k(nodes[pipe["to"]], pipe, downstream_outlet, nodes)
            height = _shape(pipe)[2]
            pressure = downstream["hgl_m"] >= reach["outlet_invert_m"] + height or reach["exceeds_capacity"]
            if not _shape(pipe)[4] and downstream["hgl_m"] > reach["outlet_invert_m"] + height:
                raise CivilError("open_channel_overtopping", "Use an overflow/storage boundary model; an open section cannot retain surcharge.", element_id=pid)
            hydraulic_depth = height if pressure else reach["depth_m"]
            a, _, r, _ = _geometry(pipe, hydraulic_depth)
            v = reach["flow_m3s"] / a if a else 0.0
            sf = (pipe["n"] * reach["flow_m3s"] / (a * r ** (2 / 3))) ** 2 if a else 0.0
            vh = v * v / (2 * G)
            vh_out = results[downstream_outlet["id"]]["hydraulic_velocity_ms"] ** 2 / (2 * G) if downstream_outlet else vh
            loss = k * vh_out
            local_egl = reach["inlet_invert_m"] + hydraulic_depth + vh
            egl = max(local_egl, downstream["egl_m"] + sf * reach["length_m"] + loss)
            hgl = egl - vh
            if not _shape(pipe)[4] and hgl > reach["inlet_invert_m"] + height + 1e-9:
                raise CivilError("open_channel_overtopping", "Use an overflow/storage boundary model; an open section cannot retain surcharge.", element_id=pid)
            reach.update(hgl_upstream_m=hgl, hgl_downstream_m=downstream["hgl_m"],
                         egl_upstream_m=egl, egl_downstream_m=downstream["egl_m"],
                         friction_slope=sf, headloss_m=sf * reach["length_m"],
                         hydraulic_velocity_ms=v,
                         junction_loss_m=loss, junction_loss_k=k, junction_loss_reference=k_source,
                         surcharged=(hgl > reach["inlet_invert_m"] + height + 1e-9 or downstream["hgl_m"] > reach["outlet_invert_m"] + height + 1e-9))
        crowns = [results[p]["outlet_invert_m"] + _shape(pipes[p])[2] for p in incoming[nid]]
        crowns += [results[p]["inlet_invert_m"] + _shape(pipes[p])[2] for p in outgoing[nid]]
        rim = node.get("rim")
        rim = _number(rim, "rim") if rim is not None else None
        node_results[nid] = {"hgl_m": hgl, "egl_m": egl, "flow_m3s": node_flows[nid],
                             "surcharged": bool(crowns) and hgl > min(crowns) + 1e-9,
                             "flooding": rim is not None and hgl > rim + 1e-9}
    out = _result("steady Manning network and preliminary K-loss HGL/EGL", conduits=results, nodes=node_results,
                  total_inflow_m3s=sum(_number(n.get("inflow_m3s", 0), "inflow_m3s", nonnegative=True) for n in nodes.values()) + sum((flows or {}).values()),
                  total_outflow_m3s=sum(node_flows[n] for n in order if not outgoing[n]))
    out["warnings"] = warnings
    out["assumptions"] = ["Peak inflows simply add; peaks are simultaneous, with no hydrograph routing/storage/attenuation.",
                          "outfall_tailwater is depth above the outfall node invert; boundary is max(tailwater, normal depth, critical depth).",
                          "Velocity-head coefficient alpha=1. Normal-depth resistance below crown; full Manning section for pressure-flow screening.",
                          "No free-outfall exit loss unless explicit outfall loss_k; simplified K values are preliminary only."]
    checked = check(network, out, z_at=z_at, criteria=criteria)
    out["checks"] = checked["checks"]
    out["warnings"].extend(checked["warnings"])
    out["assumptions"].extend(checked["assumptions"])
    return out


def _limits(criteria):
    from .criteria import resolve
    return resolve(criteria)


def _table_value(table, diameter, *, upper=False):
    if not table:
        raise CivilError("missing_criterion_table")
    if upper:
        for bound, value in table:
            if diameter <= bound:
                return value
        return table[-1][1]
    selected = table[0][1]
    for bound, value in table:
        if diameter + 1e-12 >= bound:
            selected = value
        else:
            break
    return selected


def check(network: dict, results: dict, z_at: Callable | None = None, criteria: dict | None = None) -> dict:
    """Profile-driven slope/velocity/size/spacing/cover and crown-drop checks.

    Cover is sampled at every polyline vertex and at intervals <=10 m, against
    interpolated conduit invert, or at both endpoint rims if no terrain supplied.
    Terrain gaps refuse rather than silently substitute rim elevation.
    """
    limits = _limits(criteria)
    nodes, pipes, incoming, outgoing, _ = _network(network)
    rows, warnings = [], []
    def row(name, key, value, limit, passed, criterion, units):
        rows.append({"check": name, "element_id": key, "value": value, "limit": limit,
                     "units": units, "pass": bool(passed), "criterion": criterion,
                     "reference": limits["references"].get(criterion, _REFERENCE)})
    for pid, pipe in pipes.items():
        if pid not in results.get("conduits", {}):
            raise CivilError("not_found", element_id=pid, collection="results.conduits")
        result = results["conduits"][pid]
        _, width, height, _, _ = _shape(pipe)
        diameter = pipe["diameter"] if pipe["shape"] == "circular" else height
        minimum = _table_value(limits["gravity_min_slope_by_diameter"], diameter)
        row("minimum_slope", pid, result["slope"], minimum, result["slope"] >= minimum, "gravity_min_slope_by_diameter", "m/m")
        vfull = result["velocity_full_ms"]
        row("minimum_full_flow_velocity", pid, vfull, limits["gravity_min_velocity_full_ms"], vfull is not None and vfull >= limits["gravity_min_velocity_full_ms"], "gravity_min_velocity_full_ms", "m/s")
        vmax = max(result["velocity_ms"], vfull or 0)
        row("maximum_velocity", pid, vmax, limits["gravity_max_velocity_ms"], vmax <= limits["gravity_max_velocity_ms"], "gravity_max_velocity_ms", "m/s")
        row("maximum_depth_ratio", pid, result["depth_ratio"], limits["gravity_max_depth_ratio"], result["depth_ratio"] <= limits["gravity_max_depth_ratio"] and not result.get("exceeds_capacity", False), "gravity_max_depth_ratio", "1")
        if network.get("kind") in ("sanitary", "combined"):
            if pipe["shape"] != "circular":
                warnings.append(f"{pid}: sanitary minimum diameter is not defined for a non-circular section.")
            else:
                row("minimum_sanitary_diameter", pid, diameter, limits["gravity_min_diameter_sanitary_m"], diameter >= limits["gravity_min_diameter_sanitary_m"], "gravity_min_diameter_sanitary_m", "m")
        if diameter > limits["manhole_max_spacing_m"][-1][0]:
            warnings.append(f"{pid}: manhole spacing not checked: diameter is above the profile table; supply a jurisdictional override.")
        else:
            spacing = _table_value(limits["manhole_max_spacing_m"], diameter, upper=True)
            row("manhole_spacing", pid, result["length_m"], spacing, result["length_m"] <= spacing, "manhole_max_spacing_m", "m")
        a, b = nodes[pipe["from"]], nodes[pipe["to"]]
        z1, z2 = result["inlet_invert_m"], result["outlet_invert_m"]
        if z_at is None:
            if a.get("rim") is None or b.get("rim") is None:
                warnings.append(f"{pid}: cover not checked: endpoint rims and terrain are unavailable.")
                continue
            cover = min(_number(a["rim"], "rim") - z1 - height, _number(b["rim"], "rim") - z2 - height)
        else:
            points = [[a.get("x"), a.get("y")], *pipe.get("vertices", []), [b.get("x"), b.get("y")]]
            lengths = [math.dist(u, v) for u, v in zip(points, points[1:])]
            total = sum(lengths)
            if total == 0:
                raise CivilError("invalid_coordinates", element_id=pid)
            cover, travelled = math.inf, 0.0
            for u, v, span in zip(points, points[1:], lengths):
                count = max(1, math.ceil(span / 10))
                for i in range(count + 1):
                    f = i / count
                    x, y = u[0] + f * (v[0] - u[0]), u[1] + f * (v[1] - u[1])
                    ground = z_at(x, y)
                    if ground is None:
                        raise CivilError("terrain_outside_surface", element_id=pid, x=x, y=y)
                    ground = _number(ground, "ground_elevation")
                    invert = z1 + (z2 - z1) * (travelled + f * span) / total
                    cover = min(cover, ground - invert - height)
                travelled += span
        row("minimum_cover", pid, cover, limits["min_cover_m"], cover >= limits["min_cover_m"], "min_cover_m", "m")
    for nid in nodes:
        for pin in incoming[nid]:
            for pout in outgoing[nid]:
                crown_in = results["conduits"][pin]["outlet_invert_m"] + _shape(pipes[pin])[2]
                crown_out = results["conduits"][pout]["inlet_invert_m"] + _shape(pipes[pout])[2]
                drop = crown_in - crown_out
                invert_drop = results["conduits"][pin]["outlet_invert_m"] - results["conduits"][pout]["inlet_invert_m"]
                row("manhole_invert_drop", nid, invert_drop, 0.0, invert_drop >= -1e-9, "flowline_drop", "m")
                row("manhole_crown_drop", nid, drop, 0.0, drop >= -1e-9, "crown_matching", "m")
                if drop < -1e-9:
                    warnings.append(f"{nid}: outgoing crown is above incoming crown ({pin} -> {pout}); crown matching/drop requires review.")
    out = _result("jurisdictional gravity design criteria", checks=rows)
    out["warnings"] = warnings
    out["assumptions"] = ["Minimum-slope table uses the next-smaller diameter conservatively; manhole-spacing table uses its upper diameter bound.", "Terrain cover sampling <=10 m cannot certify unsampled terrain minima."]
    return out


def design_sizes(network: dict, flows: dict | None = None, criteria: dict | None = None, available_diameters=None) -> dict:
    """Pick the smallest supplied circular standard size satisfying all criteria."""
    if not isinstance(available_diameters, (list, tuple)) or not available_diameters:
        raise CivilError("invalid_available_diameters")
    diameters = sorted({_number(d, "available_diameters", positive=True) for d in available_diameters})
    limits = _limits(criteria)
    nodes, pipes, incoming, outgoing, order = _network(network)
    _, qflows = _accumulate(nodes, pipes, incoming, outgoing, order, flows)
    updated, choices = deepcopy(network), {}
    for pipe in updated.get("conduits") or []:
        if pipe["shape"] != "circular":
            raise CivilError("unsupported_design_shape", element_id=pipe["id"])
        _, _, _, slope, n = _reach(pipe, nodes)
        if slope <= 0:
            raise CivilError("adverse_slope", element_id=pipe["id"], slope=slope)
        selected = None
        for diameter in diameters:
            if network.get("kind") in ("sanitary", "combined") and diameter < limits["gravity_min_diameter_sanitary_m"]:
                continue
            if slope < _table_value(limits["gravity_min_slope_by_diameter"], diameter):
                continue
            candidate = {**pipe, "diameter": diameter}
            capacity = full_flow_capacity(candidate, slope, n)
            if capacity["velocity_ms"] < limits["gravity_min_velocity_full_ms"] or capacity["velocity_ms"] > limits["gravity_max_velocity_ms"]:
                continue
            try:
                depth = normal_depth(candidate, qflows[pipe["id"]], slope, n)
            except CivilError as error:
                if error.reason == "exceeds_capacity":
                    continue
                raise
            if depth["depth_m"] / diameter <= limits["gravity_max_depth_ratio"] and depth["velocity_ms"] <= limits["gravity_max_velocity_ms"]:
                selected = {"diameter_m": diameter, "depth_m": depth["depth_m"], "depth_ratio": depth["depth_m"] / diameter,
                            "velocity_full_ms": capacity["velocity_ms"], "flow_m3s": qflows[pipe["id"]]}
                pipe["diameter"] = diameter
                break
        if selected is None:
            raise CivilError("design_no_feasible_size", element_id=pipe["id"], available_diameters_m=diameters)
        choices[pipe["id"]] = selected
    return _result("enumerated standard-diameter Manning design", network=updated, sizes=choices)


def standard_step(channel_shape: dict, flow_m3s: float, n: float, slope: float, length: float,
                  control_depth: float, direction: str = "upstream", steps: int = 100) -> dict:
    """GVF standard-step energy balance, arithmetic mean friction slope.

    Alpha=1; prismatic positive-slope open channel. Subcritical profiles integrate
    upstream, supercritical downstream. Critical controls/jumps are not crossed.
    Station is measured in the flow direction from the control, so upstream x<0.
    """
    q, s, roughness = _flow_inputs(flow_m3s, slope, n)
    if q == 0:
        raise CivilError("zero_flow_profile")
    length = _number(length, "length", positive=True)
    y0 = _number(control_depth, "control_depth", positive=True)
    kind, _, height, _, closed = _shape(channel_shape)
    if kind == "circular" or closed:
        raise CivilError("unsupported_channel_shape", "Standard step requires an open rectangular/trapezoidal channel.")
    if direction not in ("upstream", "downstream") or isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise CivilError("invalid_profile_steps_or_direction")
    _geometry(channel_shape, y0)
    yn = normal_depth(channel_shape, q, s, roughness)["depth_m"]
    yc = critical_depth(channel_shape, q)["depth_m"]
    if abs(y0 - yc) <= 1e-8 * max(1.0, yc):
        raise CivilError("critical_control", "Start away from critical depth; a critical control needs separate treatment.")
    subcritical = y0 > yc
    if (subcritical and direction != "upstream") or (not subcritical and direction != "downstream"):
        raise CivilError("unstable_profile_direction")
    def energy(y):
        a = _geometry(channel_shape, y)[0]
        return y + q * q / (2 * G * a * a)
    def friction(y):
        a, _, r, _ = _geometry(channel_shape, y)
        return (roughness * q / (a * r ** (2 / 3))) ** 2
    def profile_class(y):
        if abs(yn - yc) < 1e-7 * max(yn, yc):
            return "C1" if y > yc else "C3"
        base = "M" if yn > yc else "S"
        return base + ("1" if y > max(yn, yc) else "2" if y > min(yn, yc) else "3")
    dx = (-1 if direction == "upstream" else 1) * length / steps
    profile = []
    y, x = y0, 0.0
    max_residual = 0.0
    for step in range(steps + 1):
        a = _geometry(channel_shape, y)[0]
        bed = -s * x
        profile.append({"x_m": x, "bed_elevation_m": bed, "depth_m": y,
                        "water_surface_m": bed + y, "egl_m": bed + energy(y),
                        "velocity_ms": q / a, "froude": _froude(channel_shape, y, q)[0],
                        "classification": profile_class(y)})
        if step == steps:
            break
        old_energy, old_sf = energy(y), friction(y)
        def balance(candidate):
            return energy(candidate) - old_energy - s * dx + 0.5 * (old_sf + friction(candidate)) * dx
        low = yc * (1 + 1e-9) if subcritical else max(yc * 1e-9, 1e-12)
        high = height if subcritical and height else max(y * 2, yc * 2, 1.0) if subcritical else yc * (1 - 1e-9)
        if subcritical and height is None:
            for _ in range(80):
                if balance(low) * balance(high) <= 0:
                    break
                high *= 2
        # Finite steps can create multiple algebraic roots. Find the root nearest
        # the preceding depth on the same flow branch, not a hydraulic jump.
        samples = [low * (high / low) ** (i / 120) for i in range(121)] + [y]
        samples = sorted(set(v for v in samples if low <= v <= high))
        brackets = [(a, b) for a, b in zip(samples, samples[1:]) if balance(a) * balance(b) <= 0]
        if not brackets:
            raise CivilError("not_converged", "Standard step cannot bracket a same-regime depth; shorten steps or specify the missing control/jump.", step=step + 1)
        a, b = min(brackets, key=lambda pair: abs((pair[0] + pair[1]) / 2 - y))
        y = brentq(balance, a, b, xtol=1e-11)
        max_residual = max(max_residual, abs(balance(y)))
        x += dx
    out = _result("GVF standard-step energy balance", profile=profile, normal_depth_m=yn,
                  critical_depth_m=yc, classification=profile[0]["classification"], converged=True,
                  max_energy_residual_m=max_residual)
    out["assumptions"] = ["Prismatic channel, steady flow, alpha=1; average endpoint Manning friction slope; no jumps or lateral inflow."]
    return out


def hydraulic_jump(shape_rect: dict, flow_m3s: float, y1: float) -> dict:
    """Rectangular conjugate depth and ΔE=(y2-y1)^3/(4 y1 y2), Chow (1959)."""
    q, _, _ = _flow_inputs(flow_m3s)
    kind, width, height, _, closed = _shape(shape_rect)
    if kind != "rectangular" or closed:
        raise CivilError("unsupported_jump_shape")
    y1 = _number(y1, "y1", positive=True)
    _geometry(shape_rect, y1)
    fr = q / (width * y1 * math.sqrt(G * y1))
    if fr <= 1:
        raise CivilError("jump_requires_supercritical_flow", froude=fr)
    y2 = y1 * (math.sqrt(1 + 8 * fr * fr) - 1) / 2
    if height is not None and y2 > height:
        raise CivilError("jump_exceeds_section", conjugate_depth_m=y2, height_m=height)
    loss = (y2 - y1) ** 3 / (4 * y1 * y2)
    return _result("rectangular hydraulic-jump momentum balance", upstream_depth_m=y1,
                   conjugate_depth_m=y2, energy_loss_m=loss, froude_upstream=fr,
                   froude_downstream=q / (width * y2 * math.sqrt(G * y2)))
