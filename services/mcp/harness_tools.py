"""Document-persisted harness authoring, routing, checks and manufacturing deliverables.

The routing/check RPCs operate on tessellated document solids; their clearance is a
surface gap. Connector frames and cable specifications are explicit engineering inputs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, NamedTuple, Optional

from services.harness import model as M, formboard as F
from services.mcp import core_bridge as cb
from services.mcp._envelopes import plain_dumps as dumps
from services.mcp.cad_common import _sanitize_output_path


def _error(exc: Exception) -> str:
    if isinstance(exc, M.HarnessError):
        return _paged(exc.to_dict())
    return _paged({"ok": False, "code": getattr(exc, "code", "harness_error"), "error": str(exc)})


def _paged(payload: dict, offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
    """Page variable-length lists, including nested tables, preserving coordinate vectors."""
    start, count = max(0, int(offset)), max(1, min(int(limit), 200))
    pages = {}
    def visit(value, path):
        if isinstance(value, dict):
            return {k: visit(v, f"{path}.{k}" if path else k) for k, v in value.items()}
        if isinstance(value, list):
            if len(value) <= 16 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
                return value
            total = len(value)
            begin = max(0, int((page_offsets or {}).get(path, start if "[" not in path and path.count(".") <= 1 else 0)))
            if total > count or begin:
                pages[path] = {"total": total, "offset": begin, "next_offset": begin + count if begin + count < total else None}
            return [visit(v, f"{path}[{i}]") for i, v in enumerate(value[begin:begin+count], begin)]
        return value
    out = visit(payload, "")
    out["pagination"] = {"offset": start, "limit": count, "lists": dict(list(pages.items())[:200]),
                         "truncated": any(p["next_offset"] is not None for p in pages.values()),
                         "metadata_truncated": len(pages) > 200}
    return dumps(out)


def _rpc(method: str, params: dict) -> dict:
    result = cb.call(method, params)
    if not isinstance(result, dict):
        raise M.HarnessError("bad_engine_reply", f"{method} returned no result")
    if result.get("ok") is False:
        raise M.HarnessError(result.get("code", "engine_error"), result.get("error", f"{method} failed"), detail=result)
    return result


def _save(doc_id: str, h: dict) -> dict:
    M.recompute_derived(h)
    return M.save_harness(doc_id, h)


def _connector_frame(doc_id: str, frame: Optional[dict], component: Optional[str], attach: Optional[dict]) -> dict:
    if attach and attach.get("component"):
        if component and component != attach["component"]:
            raise M.HarnessError("ambiguous_component", "component and attach.component disagree")
        component = attach["component"]
    if frame is not None:
        if attach and attach.get("sid"):
            raise M.HarnessError("ambiguous_frame", "give either frame or attach.sid, not both")
        return M.make_frame(frame.get("origin"), frame.get("normal"), frame.get("up"))
    if not attach or not attach.get("sid"):
        raise M.HarnessError("frame_required", "provide a world frame or attach={sid: ...} and component for assembly faces")
    from services.mcp.cad_common import _sid_match
    sid = attach["sid"]
    if component:
        from services.mcp.cad_asm_tools import component_source_file, open_component_part, _component_sid_match
        from services.mcp.cad_motion_tools import _xform_point
        tree = _rpc("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        comps = [c for c in tree.get("components", []) if component in (c.get("id"), c.get("name"))]
        if len(comps) != 1:
            raise M.HarnessError("unknown_component", "component must uniquely identify an assembly component")
        comp = comps[0]
        tf = comp.get("transform")
        if not isinstance(tf, list) or len(tf) != 16:
            raise M.HarnessError("component_pose_missing", "component has no resolved world transform")
        with open_component_part(component_source_file(comp, doc_id)) as part:
            match = _component_sid_match(part, doc_id, comp, sid)
        if not match or match.get("kind") != "face" or not match.get("centroid") or not match.get("normal"):
            raise M.HarnessError("face_frame_unavailable", "face SID must resolve to a face with centroid and normal")
        origin = _xform_point(tf, match["centroid"])
        normal = [sum(tf[4*i+j] * match["normal"][j] for j in range(3)) for i in range(3)]
    else:
        match = _sid_match(doc_id, sid)
        if not match or match.get("kind") != "face" or not match.get("centroid") or not match.get("normal"):
            raise M.HarnessError("face_frame_unavailable", "face SID must resolve to a face with centroid and normal")
        origin, normal = match["centroid"], match["normal"]
    return M.make_frame(origin, normal, attach.get("up"))


def _route_endpoint(h: dict, node_id: str) -> dict:
    """Router endpoint at a join node: a connector leaves along its exit direction; a junction
    on an existing bundle has no direction (the wire leaves the bundle with its own bend radius)."""
    node = M.get_node(h, node_id)
    out = {"pos": node["pos"]}
    if node.get("kind") == "connector":
        out["dir"] = M.connector_exit_dir(M.get_connector(h, node_id))
    return out


def _departure_point(h: dict, node: dict, radius: float) -> list:
    """Where a route leaving ``node`` first becomes free to turn: a connector's exit stub (2 x the
    bend radius along its exit direction, as the router builds it) ends there; other nodes have none."""
    if node.get("kind") != "connector":
        return list(node["pos"])
    return M.vadd(node["pos"], M.vscale(M.connector_exit_dir(M.get_connector(h, node["id"])), 2 * radius))


def _stub_mm(h: dict, seg: dict, node_id: str, radii: dict) -> float:
    """Length of the straight exit stub ``seg`` has at ``node_id``: twice the radius it was routed
    for (``radii``), or its bundle requirement when it predates the call; 0 at a node that is not a
    connector."""
    if M.get_node(h, node_id)["kind"] != "connector":
        return 0.0
    return 2 * max(seg.get("min_bend_radius_mm") or 0.0, radii.get(seg["id"], 0.0))


def _node_ref(h: dict, node_id: str) -> dict:
    node = M.get_node(h, node_id)
    ref = {"node": node_id, "kind": node["kind"], "pos": node["pos"]}
    if node["kind"] == "connector":
        ref["connector"] = M.get_connector(h, node_id)["ref"]
    return ref


def _mm(value: Any) -> str:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return f"{value:.6g} mm" if ok else "an unknown length"


def _route_hint(reply: dict, refs: dict) -> Optional[str]:
    """Actionable reading of the engine's blocked-endpoint, blocked-exit and budget replies."""
    code, endpoint = reply.get("code"), reply.get("endpoint")
    ref = refs.get(endpoint) or {}
    who = f"{endpoint} {ref.get('connector') or ref.get('node')}" if ref else "an endpoint"
    obstacle = reply.get("obstacle") or "an obstacle"
    if code == "budget":
        raise_nodes = "raise max_nodes (at most 200000) or " if (reply.get("max_nodes") or 0) < 200000 else ""
        return (f"search budget exhausted ({reply.get('expanded_nodes')} of {reply.get('max_nodes')} nodes at "
                f"grid {_mm(reply.get('grid_mm'))}); {raise_nodes}use a larger grid_mm")
    if code in ("start_blocked", "end_blocked"):
        text = (f"{who} is blocked by {obstacle}: {_mm(reply.get('distance_mm'))} from its surface, "
                f"needs {_mm(reply.get('required_mm'))} (clearance + OD/2)")
        if reply.get("inside_solid"):
            text += "; the endpoint lies inside the solid"
    elif code == "exit_blocked":
        text = (f"{who}: the straight exit stub needs {_mm(reply.get('stub_mm'))} along {reply.get('dir')} "
                f"but only {_mm(reply.get('free_mm'))} is clear before {obstacle}")
    else:
        return reply.get("hint")
    if ref.get("kind", "connector") != "connector":
        return text + ". That endpoint is a junction on an existing bundle: re-path the wires that use it with reroute=True"
    return (text + ". Fix the connector with harness_edit(action='edit_connector', connector_id=..., "
            "frame={origin, normal} and/or exit_dir=[x, y, z]); do not create new harnesses or scan probe routes")


def _with_context(exc: "M.HarnessError", label: str, hint: Optional[str] = None, **fields: Any) -> "M.HarnessError":
    """The engine's refusal prefixed with its cause; the engine reply stays in ``detail``."""
    if exc.extra.get("detail") is not None:
        fields["detail"] = exc.extra["detail"]
    return M.HarnessError(exc.code, f"{label}: {exc}", hint=exc.hint or hint, **fields)


def _check_segment(h: dict, doc_id: str, seg: dict, clearance: float, keepouts: list, ignore: dict) -> dict:
    """harness_path_check of one segment against its own OD and bend radius (0 when none is known).

    The path carries through-continuation context (``model.segment_check_path``) so an arc cut by
    a breakout or clamp is seen whole; ``report_range`` limits findings to the segment itself, and
    finding indices and ``length_mm`` are returned relative to the segment's own path.
    """
    path, report = M.segment_check_path(h, seg)
    radius = seg.get("min_bend_radius_mm")
    check = _rpc("harness_path_check", {"doc_id": doc_id, "path": path, "od_mm": seg["od_mm"],
        "min_bend_radius_mm": radius if radius is not None else 0.0, "clearance_mm": clearance,
        "keepouts": keepouts, **({"report_range": report} if report else {}), **ignore})
    if report:
        for key in ("bend_violations", "clearance_violations"):
            if key in check:
                check[key] = [{**item, "index": item["index"] - report[0]} if "index" in item else item
                              for item in check[key]]
        if "report_length_mm" in check:
            check["length_mm"] = check.pop("report_length_mm")
    return check


def _through_connector(h: dict, start: str, goal: str) -> bool:
    """True when wires going from ``start`` to ``goal`` through the tree pass through a connector that is
    not their end: both its segments leave along its exit direction, a 180 degree reversal."""
    if start == goal:
        return False
    path = M.tree_path(h, start, goal) or []
    return any(M.get_node(h, n)["kind"] == "connector"
               for n in [start] + [s["b"] if fwd else s["a"] for s, fwd in path[:-1]])


def _joinable_nodes(h: dict, component: int, comps: dict, ends: set) -> list:
    """Nodes of a tree a join may use: those from which the joining wires reach their own connectors
    (``ends``) without running through another one, so a connector qualifies only as the end of every
    wire using the join."""
    return [n for n in h["nodes"] if comps.get(n["id"]) == component
            and not any(_through_connector(h, n["id"], c) for c in ends)]


class _Attach(NamedTuple):
    """Where a join can attach: a ``node`` or a point on a ``segment``. ``bad`` marks a point where the
    joining wires are estimated unable to turn; any point that is not bad ranks before it."""
    bad: bool
    distance: float
    kind: str
    id: str
    point: list


def _tap_ways(h: dict, seg: dict, ends: set) -> Optional[set]:
    """The ways (``"a"``, ``"b"``: toward that end of ``seg``) the joining wires continue from a tap on
    ``seg`` to reach their own connectors (``ends``); None when one of them would run through another
    connector wherever it joined."""
    ways = set()
    for c in ends:
        runs_on = (M.tree_path(h, seg["a"], c) or [(None, None)])[0][0] is seg  # c lies beyond seg.b
        if _through_connector(h, seg["b"] if runs_on else seg["a"], c):
            return None
        ways.add("b" if runs_on else "a")
    return ways


def _leg_points(h: dict, seg: dict, target: list, margin: float, radii: dict, ways: set) -> list:
    """``_Attach`` for the point nearest ``target`` on each straight leg of ``seg`` that can take a junction.

    A wire turning at a junction bends with its own radius within the adjacent straight runs, so the
    point keeps ``margin`` from both leg ends (an arc chord or a corner can never pass the junction
    check), and it lies beyond the exit stub of a connector end: a bundle leaves its connector
    straight, and a junction inside the stub makes its wires turn there or double back. A wire that
    arrives from ``target`` and continues along ``ways`` turns through the angle between the two
    directions and needs ``margin x tan(turn / 2)`` of run to do it: points where the nearest one
    leaves too little run give way to the best of a few others along the leg, and are marked ``bad``
    when none does.
    """
    path, total = seg["path"], M.seg_length(seg)
    stub_a, stub_b = _stub_mm(h, seg, seg["a"], radii), _stub_mm(h, seg, seg["b"], radii)
    out, walked = [], 0.0
    for a, b in zip(path, path[1:]):
        length = M.vdist(a, b)
        before, walked = walked, walked + length
        lo, hi = max(margin, stub_a - before), length - max(margin, stub_b - (total - walked))
        if length <= 0 or hi < lo:
            continue
        along = M.vdot(M.vsub(target, a), M.vsub(b, a)) / length
        heading = M.vscale(M.vsub(b, a), 1 / length)

        def shortfall(s: float) -> float:
            """Run the joining wires lack at ``s`` mm along the leg (0 when each way has enough)."""
            arrive = M.vsub(M.vadd(a, M.vscale(heading, s)), target)
            if M.vnorm(arrive) < 1e-9:
                return 0.0
            arrive = M.vunit(arrive)
            lack = 0.0
            for way in ways:
                leave, run = (heading, length - s) if way == "b" else (M.vscale(heading, -1), s)
                turn = math.acos(max(-1.0, min(1.0, M.vdot(arrive, leave))))
                lack = max(lack, margin * math.tan(turn / 2) - run if turn < math.pi - 1e-6 else math.inf)
            return lack

        nearest = min(max(along, lo), hi)
        options = [nearest] + [lo + (hi - lo) * k / 8 for k in range(9)]
        fits = [s for s in options if shortfall(s) <= 0]
        s = min(fits, key=lambda v: abs(v - nearest)) if fits else min(options, key=shortfall)
        point = M.vadd(a, M.vscale(heading, s))
        out.append(_Attach(not fits, M.vdist(point, target), "segment", seg["id"], point))
    return out


def _sites(h: dict, component: int, comps: dict, ends: set) -> tuple:
    """What a tree offers a join: ``(joinable nodes, [(segment, ways)])`` for the segments it may be
    tapped on (``_tap_ways``), for the joining wires' own connectors ``ends``."""
    taps = [(seg, ways) for seg in h["segments"] if comps.get(seg["a"]) == component
            and (ways := _tap_ways(h, seg, ends)) is not None]
    return _joinable_nodes(h, component, comps, ends), taps


def _nearest_attachment(h: dict, sites: tuple, target: list, margin: float, radii: dict) -> Optional[_Attach]:
    """The best attachment of a tree's ``sites`` to ``target``: the nearest joinable node, or point on a
    tappable segment (``_leg_points``), that is not ``bad``. None when the tree offers neither."""
    nodes, taps = sites
    found = [_Attach(False, M.vdist(n["pos"], target), "node", n["id"], n["pos"]) for n in nodes]
    for seg, ways in taps:
        found += _leg_points(h, seg, target, margin, radii, ways)
    return min(found, key=lambda v: v[:2], default=None)


def _best_join(h: dict, comps: dict, sides: list, radius: float, margin: float, radii: dict) -> Optional[tuple]:
    """The closest attachment between two trees, ``(anchor_id, attachment)``: every joinable node of
    one tree (a connector measured from its departure point) against the nearest attachment of the
    other, both ways round, so a bundle can be tapped mid-leg whichever tree holds the joining wires'
    own connector. ``sides`` is ``[(component, ends)]`` for the two trees."""
    sites = [_sites(h, component, comps, ends) for component, ends in sides]
    best = None
    for near, far in ((0, 1), (1, 0)):
        for node in sites[near][0]:
            found = _nearest_attachment(h, sites[far], _departure_point(h, node, radius), margin, radii)
            if found is not None and (best is None or found[:2] < best[1][:2]):
                best = (node["id"], found)
    return best


def _resolve_candidate(h: dict, cand: _Attach) -> str:
    return cand.id if cand.kind == "node" else M.split_segment_at(h, cand.id, cand.point)


def _no_join_point(wire: str, wires: list, margin: float, where: str) -> "M.HarnessError":
    return M.HarnessError(
        "no_join_point", f"{wire}: {where} has no point where wires {wires} can join it",
        hint="a wire joins a bundle at one of its own connectors, a junction node or a straight run of at least "
             f"{_mm(2 * margin)} beyond every connector exit stub (2 x the bundle's bend radius); give the bundles "
             "more straight length (move a connector or change its exit_dir)",
        wire=wire, wires=wires)


def _bundle_note(wires: list, radius: float) -> str:
    return (f"The bundle on this path carries {len(wires)} wires ({', '.join(wires)}) once every join is made and "
            f"needs a {_mm(radius)} centerline bend radius with {_mm(2 * radius)} exit stubs: give it more room, "
            "split the wires across separate connector pairs, or supply measured packing_factor or "
            "min_bend_radius_mm if the estimates are conservative")


#: The engine's prefer_paths limits (core/harness/route.cpp) and the simplification tolerance used to
#: stay within them: arc samples are dropped, preference only needs a leg within one grid cell.
_PREFER_MAX_LINES, _PREFER_MAX_LEGS, _PREFER_TOL_MM = 128, 256, 0.5


def _preferred_paths(h: dict, start_id: str, end_id: str) -> "tuple[list, bool]":
    """Simplified centrelines of the two trees being joined, nearest first, within engine limits.

    Returns ``(paths, truncated)``.
    """
    comps = M.components(h)
    joined = {comps[start_id], comps[end_id]}
    ends = [M.get_node(h, start_id)["pos"], M.get_node(h, end_id)["pos"]]
    lines = sorted(
        (M.simplify_polyline(s["path"], _PREFER_TOL_MM) for s in h["segments"] if comps.get(s["a"]) in joined),
        key=lambda p: min(M.closest_on_polyline(e, p)[0] for e in ends))
    out, legs = [], 0
    for line in lines:
        if len(out) == _PREFER_MAX_LINES or legs + len(line) - 1 > _PREFER_MAX_LEGS:
            return out, True
        out.append(line)
        legs += len(line) - 1
    return out, False


def _segment_box_intersects(a: list, b: list, lo: list, hi: list) -> bool:
    t0, t1 = 0.0, 1.0
    for k in range(3):
        d = b[k] - a[k]
        if abs(d) < 1e-12:
            if a[k] < lo[k] or a[k] > hi[k]:
                return False
        else:
            u, v = (lo[k] - a[k]) / d, (hi[k] - a[k]) / d
            t0, t1 = max(t0, min(u, v)), min(t1, max(u, v))
            if t0 > t1:
                return False
    return True


def _motion_check(doc_id: str, h: dict, motion: dict, clearance: float) -> dict:
    from services.mcp.cad_asm_tools import run_motion_sweep
    from services.mcp.cad_motion_tools import _rigid_inverse16, _mat_mul16, _xform_point
    allowed = {"mate_id", "start", "stop", "steps"}
    if set(motion) - allowed or not {"mate_id", "start", "stop"} <= set(motion):
        raise M.HarnessError("bad_motion", "motion needs mate_id, start and stop, optionally steps")
    steps = int(motion.get("steps", 24))
    if steps < 2 or steps > 120:
        raise M.HarnessError("bad_motion", "motion steps must be between 2 and 120")
    sweep = run_motion_sweep(doc_id, mate_id=motion["mate_id"], start=motion["start"],
                             stop=motion["stop"], steps=steps, check_interference=False,
                             persist_study=False)
    frames = sweep.get("trajectory") or []
    bodies = sweep.get("reference_bodies") or []
    refs = sweep.get("reference_transforms") or {}
    if not sweep.get("ok") or len(frames) != steps or not bodies or not refs:
        raise M.HarnessError("motion_incomplete", "motion sweep did not return all reference bodies and sampled poses", detail=sweep)
    hits, missing = [], []
    for frame in frames:
        poses = {str(b.get("component")): b.get("transform") for b in frame.get("bodies", []) if b.get("component") and b.get("transform")}
        for body in bodies:
            cid = str(body.get("component") or "").split("#", 1)[0]
            box = body.get("bbox") or {}
            if not cid or cid not in refs or cid not in poses or not box.get("min") or not box.get("max"):
                missing.append({"frame": frame.get("i"), "component": cid})
                continue
            transform = _mat_mul16(poses[cid], _rigid_inverse16(refs[cid]))
            lo0, hi0 = box["min"], box["max"]
            corners = [_xform_point(transform, [hi0[k] if bits & (1 << k) else lo0[k] for k in range(3)]) for bits in range(8)]
            lo = [min(p[k] for p in corners) for k in range(3)]
            hi = [max(p[k] for p in corners) for k in range(3)]
            for seg in h["segments"]:
                if not seg.get("wires"):
                    continue
                if seg.get("od_mm") is None:
                    missing.append({"frame": frame.get("i"), "segment": seg["id"], "reason": "bundle OD not specified"})
                    continue
                margin = seg["od_mm"] / 2 + clearance
                blo = [v - margin for v in lo]
                bhi = [v + margin for v in hi]
                if any(_segment_box_intersects(a, b, blo, bhi) for a, b in zip(seg["path"], seg["path"][1:])):
                    hits.append({"frame": frame.get("i"), "component": cid, "body": body.get("body_id"), "segment": seg["id"]})
    return {"evaluated": not missing, "frames": len(frames), "possible_clearance_conflicts": hits,
            "conflict_count": len(hits), "missing_poses": missing,
            "basis": "Conservative transformed AABB test at sampled poses only; conflicts require exact follow-up. Motion between samples is not checked."}


def register_harness_tools(mcp) -> None:
    @mcp.tool()
    def harness_new(doc_id: str, name: str, harness_id: Optional[str] = None, defaults: Optional[dict] = None) -> str:
        """Create a named harness study in a document; multiple harnesses can coexist."""
        try:
            h = M.new_harness(name, harness_id, defaults, [x["id"] for x in M.list_harnesses(doc_id)])
            saved = _save(doc_id, h)
            return dumps({"ok": True, "harness_id": h["id"], "summary": M.summarize(h), **saved})
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_get(doc_id: str, harness_id: Optional[str] = None, offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Read a harness, or list harnesses by passing harness_id='*'. Lists are paginated."""
        try:
            if harness_id == "*":
                rows = M.list_harnesses(doc_id)
                return _paged({"ok": True, "harnesses": rows}, offset, limit, page_offsets)
            h = M.load_harness(doc_id, harness_id)
            M.recompute_derived(h)
            return _paged({"ok": True, "harness": h}, offset, limit, page_offsets)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_add_connector(doc_id: str, harness_id: str, ref: str, family: str, pins: int,
                              frame: Optional[dict] = None, pitch_mm: Optional[float] = None, rows: Optional[int] = None,
                              part_number: Optional[str] = None, variant: Optional[str] = None,
                              gender: Optional[str] = None, component: Optional[str] = None,
                              attach: Optional[dict] = None, exit_dir: Optional[list] = None,
                              signals: Optional[dict] = None, connector_id: Optional[str] = None) -> str:
        """Add a connector with a world frame or attach={sid:face_sid}, optionally on an assembly component."""
        try:
            h = M.load_harness(doc_id, harness_id)
            if attach and attach.get("component"):
                if component and component != attach["component"]:
                    raise M.HarnessError("ambiguous_component", "component and attach.component disagree")
                component = attach["component"]
            f = _connector_frame(doc_id, frame, component, attach)
            c, warnings = M.add_connector(h, ref=ref, family=family, pins=pins, frame=f, pitch_mm=pitch_mm,
                rows=rows, part_number=part_number, variant=variant, gender=gender, component=component,
                attach=attach, exit_dir=exit_dir, signals=signals, connector_id=connector_id)
            if attach or component:
                warnings.append("The resolved world frame is a snapshot; it does not follow subsequent component motion.")
            return dumps({"ok": True, "harness_id": h["id"], "connector": c, "warnings": warnings, **_save(doc_id, h)})
        except Exception as e:
            return _error(e)

    def add_wire_impl(doc_id, harness_id, from_, to, insulation_od_mm, gauge_awg, csa_mm2, kind,
                      color, net, signal, group, signal_class, shield_ground_end, service_loop_mm,
                      strip_mm, min_bend_radius_mm, bend_radius_x_od, wire_id, allow_shared_pin, extra, insulation,
                      ribbon_width_mm, ribbon_thickness_mm):
        h = M.load_harness(doc_id, harness_id)
        wire, warnings = M.add_wire(h, from_=from_, to=to, insulation_od_mm=insulation_od_mm,
            gauge_awg=gauge_awg, csa_mm2=csa_mm2, kind=kind, color=color, net=net, signal=signal,
            group=group, signal_class=signal_class, shield_ground_end=shield_ground_end,
            service_loop_mm=service_loop_mm, strip_mm=strip_mm, min_bend_radius_mm=min_bend_radius_mm,
            bend_radius_x_od=bend_radius_x_od, wire_id=wire_id, allow_shared_pin=allow_shared_pin,
            extra=extra, insulation=insulation, ribbon_width_mm=ribbon_width_mm,
            ribbon_thickness_mm=ribbon_thickness_mm)
        return dumps({"ok": True, "harness_id": h["id"], "wire": wire, "warnings": warnings, **_save(doc_id, h)})

    @mcp.tool()
    def harness_add_wire(doc_id: str, harness_id: str, from_: Any, to: Any, insulation_od_mm: Optional[float] = None,
                         gauge_awg: Any = None, csa_mm2: Optional[float] = None, kind: str = "wire",
                         color: Optional[str] = None, net: Optional[str] = None, signal: Optional[str] = None,
                         group: Optional[str] = None, signal_class: Optional[str] = None,
                         shield_ground_end: Optional[str] = None, service_loop_mm: Optional[float] = None,
                         strip_mm: Optional[float] = None, min_bend_radius_mm: Optional[float] = None,
                         bend_radius_x_od: Optional[float] = None, wire_id: Optional[str] = None,
                         allow_shared_pin: bool = False, extra: Optional[dict] = None,
                         insulation: Optional[str] = None, ribbon_width_mm: Optional[float] = None,
                         ribbon_thickness_mm: Optional[float] = None) -> str:
        """Connect two connector:pin endpoints. Supply AWG or CSA and insulated OD.

        insulation may name PVC, PTFE, FEP, ETFE, XLPE, silicone or TPE.
        For ribbon, supply width and thickness instead of OD; routing uses a circular
        envelope and does not model ribbon orientation.
        Minimum bend radius is a centerline radius; a sourced estimate is used when omitted.
        """
        try:
            return add_wire_impl(doc_id, harness_id, from_, to, insulation_od_mm, gauge_awg, csa_mm2, kind,
                color, net, signal, group, signal_class, shield_ground_end, service_loop_mm, strip_mm,
                min_bend_radius_mm, bend_radius_x_od, wire_id, allow_shared_pin, extra, insulation,
                ribbon_width_mm, ribbon_thickness_mm)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_connect(doc_id: str, harness_id: str, from_: Any, to: Any, insulation_od_mm: Optional[float] = None,
                        gauge_awg: Any = None, csa_mm2: Optional[float] = None, kind: str = "wire",
                        color: Optional[str] = None, signal_class: Optional[str] = None,
                        shield_ground_end: Optional[str] = None, min_bend_radius_mm: Optional[float] = None,
                        bend_radius_x_od: Optional[float] = None, service_loop_mm: Optional[float] = None,
                        strip_mm: Optional[float] = None, insulation: Optional[str] = None,
                        ribbon_width_mm: Optional[float] = None, ribbon_thickness_mm: Optional[float] = None) -> str:
        """Convenience pin-to-pin connection; for full wire metadata use harness_add_wire."""
        try:
            return add_wire_impl(doc_id, harness_id, from_, to, insulation_od_mm, gauge_awg, csa_mm2, kind,
                color, None, None, None, signal_class, shield_ground_end, service_loop_mm, strip_mm,
                min_bend_radius_mm, bend_radius_x_od, None, False, None, insulation,
                ribbon_width_mm, ribbon_thickness_mm)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_from_netlist(doc_id: str, harness_id: str, schematic_id: str, mapping: dict,
                             wire_spec: dict, nets: Optional[list] = None, allow_shared_pin: bool = False,
                             offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Seed wires from a schematic. Map each 'REF:pin' to an endpoint or {connector:{definition},pin:n}.

        All members on referenced connector refs in selected nets must be mapped; no pinout is inferred.
        Multi-terminal nets require explicit allow_shared_pin=True to make a star at the first mapped pin.
        """
        try:
            from services.mcp.ecad_common import _get_sch, netlist_mod
            h = M.load_harness(doc_id, harness_id)
            nl = netlist_mod.build_netlist(_get_sch(schematic_id).doc)
            netmap = nl.get("nets") or {}
            if not mapping or not isinstance(mapping, dict):
                raise M.HarnessError("mapping_required", "mapping must map schematic REF:pin keys to harness endpoints")
            refs = {str(k).rsplit(":", 1)[0] for k in mapping}
            wanted = list(nets) if nets is not None else [n for n, members in netmap.items() if any(str(m.get("ref")) in refs for m in members)]
            missing_nets = [n for n in wanted if n not in netmap]
            if missing_nets:
                raise M.HarnessError("unknown_net", "selected nets are absent from the schematic", nets=missing_nets)
            prepared = []
            for name in wanted:
                members = [f"{m.get('ref')}:{m.get('pin')}" for m in netmap[name] if str(m.get("ref")) in refs]
                missing = [p for p in members if p not in mapping]
                if missing:
                    raise M.HarnessError("unmapped_pins", f"net {name} has unmapped connector pins", pins=missing)
                if len(members) < 2:
                    raise M.HarnessError("unmapped_net", f"net {name} has fewer than two mapped connector endpoints", hint="Map both physical cable endpoints or exclude this net explicitly.")
                if len(members) > 2 and not allow_shared_pin:
                    raise M.HarnessError("net_topology_required", f"net {name} has more than two mapped endpoints", hint="Model the physical splice explicitly, or opt into a shared-pin star with allow_shared_pin=True.")
                prepared.append((name, members))
            if not prepared:
                raise M.HarnessError("no_nets", "no selected connector nets to import")
            allowed = {"insulation_od_mm", "gauge_awg", "csa_mm2", "kind", "color", "signal_class", "shield_ground_end", "service_loop_mm", "strip_mm", "min_bend_radius_mm", "bend_radius_x_od", "extra", "insulation", "ribbon_width_mm", "ribbon_thickness_mm"}
            if set(wire_spec) - allowed:
                raise M.HarnessError("bad_wire_spec", "unknown wire_spec fields", fields=sorted(set(wire_spec)-allowed))
            mapping = dict(mapping)
            created, definitions, warnings = [], {}, []
            for key, target in list(mapping.items()):
                if not isinstance(target, dict) or not isinstance(target.get("connector"), dict):
                    continue
                definition = dict(target["connector"])
                ref = definition.get("ref")
                if not ref or "pin" not in target:
                    raise M.HarnessError("bad_mapping", f"{key}: connector definition needs ref and endpoint pin")
                if ref in definitions and definitions[ref] != definition:
                    raise M.HarnessError("conflicting_connector", f"conflicting definitions for {ref}")
                if ref not in definitions:
                    definitions[ref] = dict(definition)
                    f = _connector_frame(doc_id, definition.pop("frame", None), definition.get("component"), definition.get("attach"))
                    c, notes = M.add_connector(h, frame=f, **definition)
                    created.append(c["id"])
                    warnings.extend(notes)
                    if definition.get("attach") or definition.get("component"):
                        warnings.append(f"{ref}: resolved frame is a snapshot and does not follow subsequent component motion.")
                mapping[key] = {"connector": ref, "pin": target["pin"],
                                **({"treatment": target["treatment"]} if "treatment" in target else {})}
            made = []
            for name, members in prepared:
                for other in members[1:]:
                    kw = dict(wire_spec)
                    if "signal_class" not in kw and name.upper() in M.GROUND_NET_NAMES:
                        kw["signal_class"] = "ground"
                        warnings.append(f"{name}: ground signal class inferred from net name (heuristic)")
                    w, notes = M.add_wire(h, from_=mapping[members[0]], to=mapping[other], net=name,
                                           allow_shared_pin=allow_shared_pin, **kw)
                    made.append(w["id"])
                    warnings.extend(notes)
            h["last_netlist_import"] = {"connectors": created, "wires": made, "warnings": warnings}
            return _paged({"ok": True, "harness_id": h["id"], "connectors": created, "wires": made,
                           "total_wires": len(made), "warnings": warnings,
                           "pagination_hint": "Retrieve further pages with harness_get and page_offsets targeting harness.last_netlist_import; do not repeat the import.",
                           **_save(doc_id, h)}, offset, limit, page_offsets)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_route(doc_id: str, harness_id: Optional[str] = None, wire_ids: Optional[list] = None,
                      clearance_mm: Optional[float] = None, keepouts: Optional[list] = None,
                      ignore_bodies: Optional[list] = None, grid_mm: Optional[float] = None,
                      max_nodes: Optional[int] = None, endpoint_relax_mm: Optional[float] = None,
                      packing_factor: Optional[float] = None, reroute: bool = False,
                      offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Route selected wires through the engine, reusing the existing forest and splitting breakouts.

        A route that joins an existing bundle leaves it at a junction (never inside a connector's exit
        stub); the full-wire check judges that junction as a fillet of the wire's own bend radius. The
        join point is the one nearest each connector's departure point (origin plus exit_dir x 2 x bend
        radius), so a connector that exits away from a bundle attaches ahead of its exit. When joins
        grow a bundle past the requirement it was routed for, every route is made again, each planned
        for the largest bundle it carried (reported as repathed_segments and route_passes); a bundle
        with no room at that size fails naming its wires and required radius. Bundles this call did
        not route are not re-pathed and fail with predates_call.
        Wires whose connectors are already joined are skipped; reroute=True first removes the
        segments carrying the selected wires so they are re-pathed (use it after geometry, keepout
        or connector changes). A segment shared with unselected wires is refused, listing them.
        Failures name the wire, endpoints and obstacle (blocked endpoints give distance_mm,
        required_mm and free_mm): fix the connector with harness_edit(action='edit_connector')
        instead of probing with more routes.
        Obstacles are the document's bodies; ignore_bodies lists body ids, names or component
        names to leave out. keepouts (stored on the harness) are a list of
        {"aabb": [[xmin,ymin,zmin],[xmax,ymax,zmax]], "name"?: str, "clearance_mm"?: float} boxes or
        {"body": id|name|component, "clearance_mm"?: float} per-body clearance overrides (not for an
        ignored body). grid_mm is the search cell (default from OD, bend radius and endpoint distance);
        max_nodes is the search budget, an integer 1..200000 (default 50000): when it is exhausted at
        200000 use a larger grid_mm.
        """
        try:
            h = M.load_harness(doc_id, harness_id)
            if packing_factor is not None:
                pf = float(packing_factor)
                if not math.isfinite(pf) or not 0 < pf <= 1:
                    raise M.HarnessError("bad_packing_factor", "packing_factor must be in (0, 1]")
                h["defaults"]["packing_factor"] = pf
                h["packing_factor_basis"] = "caller supplied"
            if keepouts is not None:
                h["keepouts"] = keepouts
            M.recompute_derived(h)
            selected = [M.get_wire(h, str(w)) for w in wire_ids] if wire_ids is not None else h["wires"]
            if not selected:
                raise M.HarnessError("no_wires", "no wires selected")
            # Existing tree members can gain another wire when components join.
            missing = [w["id"] for w in h["wires"] if w.get("min_bend_radius_mm") is None]
            if missing:
                raise M.HarnessError("bend_radius_required", "routing needs a stated minimum bend radius for every wire", wires=missing, hint="Set each wire's datasheet radius or explicit bend-radius multiplier.")
            clearance = h["defaults"]["clearance_mm"] if clearance_mm is None else float(clearance_mm)
            if not math.isfinite(clearance) or clearance < 0:
                raise M.HarnessError("bad_clearance", "clearance_mm must be finite and nonnegative")
            removed = M.remove_segments_of_wires(h, [w["id"] for w in selected]) if reroute else None
            selected_ids = {w["id"] for w in selected}
            keep = keepouts if keepouts is not None else h.get("keepouts") or []
            ignore = {"ignore_bodies": ignore_bodies} if ignore_bodies is not None else {}
            baseline = copy.deepcopy(h)  # where every pass starts
            # The segments this pass created (halves of later splits included) and what each was routed
            # for: ``join`` is the index of the route that made it; ``od`` and ``radius`` the bundle planned.
            made: dict = {}
            # What the route with a given index must be planned for: the largest bundle a route of that index
            # finally carried in an earlier pass. Route k joins the same two trees (connector sets) in every
            # pass whatever it attaches to, so the index identifies its own wires, and floors only grow.
            floors: dict = {}
            repathed: list = []
            warnings = ["Bundle OD is an estimate, not a verified enclosing diameter; confirm the physical bundle. Twisted groups require their actual assembled OD."]

            def route_pass(passes: int) -> None:
                joins = 0
                for w in selected:
                    a, b = w["from"]["connector"], w["to"]["connector"]
                    comps = M.components(h)
                    if comps[a] == comps[b]:
                        continue
                    # The bundle every wire that can use the joining path will form, exactly as the
                    # later check sizes it, so the routed arc radius meets that requirement.
                    active = [x for x in h["wires"] if {comps[x["from"]["connector"]], comps[x["to"]["connector"]]} == {comps[a], comps[b]}]
                    need = M.bundle_requirements(active, h["defaults"]["packing_factor"])
                    if need["od_mm"] is None:
                        raise M.HarnessError("packing_factor_required", "bundle packing factor is explicitly unknown",
                                             hint="Supply packing_factor for this route or edit the harness default.")
                    od, radius = need["od_mm"], need["min_bend_radius_mm"]
                    floor = floors.get(joins)
                    if floor:  # an earlier pass found this route's bundle grows past its own wires
                        od, radius = max(od, floor["od"]), max(radius, floor["radius"])
                    margin = max(x["min_bend_radius_mm"] for x in active)
                    # The joining wires' own connectors on each side: only those may be a join end, so
                    # no wire runs through another wire's connector.
                    def side(comp: int) -> tuple:
                        return comp, {e["connector"] for x in active for e in (x["from"], x["to"])
                                      if comps.get(e["connector"]) == comp}
                    # Attach where the gap between the two trees is smallest, measured from each
                    # connector's departure point (its exit stub ends there), segment interiors included.
                    joined = _best_join(h, comps, [side(comps[a]), side(comps[b])], radius, margin,
                                        {sid: e["radius"] for sid, e in made.items()})
                    if joined is None:
                        raise _no_join_point(w["id"], [x["id"] for x in active], margin, "either tree")
                    start_id, found = joined
                    parent = made.get(found.id) if found.kind == "segment" else None
                    nodes_before = {n["id"] for n in h["nodes"]}
                    end_id = _resolve_candidate(h, found)
                    if end_id not in nodes_before:  # the attachment segment was split at end_id
                        made.pop(found.id, None)
                        if parent is not None:
                            for s in h["segments"]:
                                if end_id in (s["a"], s["b"]):
                                    made[s["id"]] = dict(parent)
                    prefer, prefer_truncated = _preferred_paths(h, start_id, end_id)
                    if prefer_truncated:
                        warnings.append(f"{w['id']}: only the {len(prefer)} existing centrelines nearest its endpoints "
                                        "were offered as bundling preferences (engine limit).")
                    params = {"doc_id": doc_id, "start": _route_endpoint(h, start_id), "end": _route_endpoint(h, end_id),
                              "od_mm": od, "min_bend_radius_mm": radius, "clearance_mm": clearance,
                              "keepouts": keep, "prefer_paths": prefer}
                    for key, val in (("ignore_bodies", ignore_bodies), ("grid_mm", grid_mm), ("max_nodes", max_nodes), ("endpoint_relax_mm", endpoint_relax_mm)):
                        if val is not None:
                            params[key] = val
                    try:
                        routed = _rpc("harness_route", params)
                    except M.HarnessError as exc:
                        refs = {"start": _node_ref(h, start_id), "end": _node_ref(h, end_id)}
                        hint = _route_hint(exc.extra.get("detail") or {}, refs)
                        fields = {}
                        if floor:  # no room for the bundle as it grew
                            hint = ". ".join(filter(None, (hint, _bundle_note(floor["wires"], radius))))
                            fields = {"bundle_wires": floor["wires"], "od_mm": od, "min_bend_radius_mm": radius,
                                      "route_passes": passes}
                        raise _with_context(exc, w["id"], hint, wire=w["id"], start=refs["start"], end=refs["end"],
                                            **fields) from exc
                    path = routed.get("path") or []
                    if len(path) < 2:
                        raise M.HarnessError("bad_engine_reply", "router returned no usable path")
                    seg = M.add_segment(h, start_id, end_id, path)
                    made[seg["id"]] = {"join": joins, "od": od, "radius": radius}
                    joins += 1
                    warnings.extend(routed.get("warnings") or [])
                    M.recompute_derived(h)

            def failing_bundles() -> list:
                """Recheck the final enlarged bundles, not just the newly routed centerlines."""
                bad = []
                for seg in h["segments"]:
                    if not seg.get("wires"):
                        continue
                    if seg.get("od_mm") is None:
                        raise M.HarnessError("packing_factor_required", f"bundle {seg['id']} has no usable outer diameter",
                                             hint="Supply a packing factor or a segment OD override.")
                    try:
                        check = _check_segment(h, doc_id, seg, clearance, keep, ignore)
                    except M.HarnessError as exc:
                        raise _with_context(exc, f"bundle {seg['id']}", segment=seg["id"], wires=seg["wires"]) from exc
                    if check.get("bend_violations") or check.get("clearance_violations"):
                        bad.append((seg, check))
                return bad

            def bundle_failure(seg: dict, check: dict, passes: int) -> "M.HarnessError":
                tried = {"repathed_segments": repathed, "route_passes": passes} if repathed else {}
                if seg["id"] in made:
                    return M.HarnessError("route_check_failed", f"final bundle {seg['id']} violates bend or clearance",
                                          hint=_bundle_note(seg["wires"], seg["min_bend_radius_mm"]) if repathed else None,
                                          segment=seg["id"], wires=seg["wires"], od_mm=seg["od_mm"],
                                          min_bend_radius_mm=seg["min_bend_radius_mm"], detail=check, **tried)
                return M.HarnessError(
                    "route_check_failed",
                    f"existing bundle {seg['id']}, which this call did not route, no longer meets its bend or clearance requirement",
                    hint=f"{seg['id']} predates this call (older geometry, or sized for fewer wires): re-path it with "
                         f"harness_route(reroute=True, wire_ids={seg['wires']})",
                    segment=seg["id"], wires=seg["wires"], predates_call=True, detail=check, **tried)

            passes = 0
            while True:
                passes += 1
                route_pass(passes)
                failing = failing_bundles()
                if not failing:
                    break
                # A bundle that outgrew what its route was planned for (a join added wires) raises that
                # route's floor, and every route is made again from the start: each pass either grows a
                # floor, which is bounded by the bundle of every selected wire, or ends here. Any other
                # failure, or a bundle holding wires this call may not unroute or author overrides it
                # would discard, cannot be repaired by re-pathing.
                for seg, check in failing:
                    entry = made.get(seg["id"])
                    outgrown = entry is not None and (seg["od_mm"] > entry["od"] + 1e-9
                                                      or seg["min_bend_radius_mm"] > entry["radius"] + 1e-9)
                    if not outgrown or not set(seg["wires"]) <= selected_ids or M.segment_fields(seg):
                        raise bundle_failure(seg, check, passes)
                    floor = floors.setdefault(entry["join"], {"od": 0.0, "radius": 0.0, "wires": seg["wires"]})
                    if seg["min_bend_radius_mm"] >= floor["radius"]:
                        floor["wires"] = list(seg["wires"])
                    floor["od"], floor["radius"] = max(floor["od"], seg["od_mm"]), max(floor["radius"], seg["min_bend_radius_mm"])
                repathed.extend(s["id"] for s in h["segments"] if s["id"] in made)
                h.clear()
                h.update(copy.deepcopy(baseline))
                made.clear()
                del warnings[1:]
            # Check full wire paths too: a breakout can introduce a turn between segments.
            for wire in h["wires"]:
                path, junctions = M.wire_route(h, wire)
                if not path:
                    continue
                try:
                    check = _rpc("harness_path_check", {"doc_id": doc_id, "path": path,
                        "od_mm": wire["insulation_od_mm"], "min_bend_radius_mm": wire["min_bend_radius_mm"],
                        "clearance_mm": clearance, "keepouts": keep,
                        **({"junctions": junctions} if junctions else {}), **ignore})
                except M.HarnessError as exc:
                    raise _with_context(exc, f"wire {wire['id']}", wire=wire["id"]) from exc
                if check.get("bend_violations"):
                    sharing = [x["id"] for x in h["wires"] if set(x["segments"]) & set(wire["segments"])]
                    worst = check["bend_violations"][0]
                    raise M.HarnessError(
                        "route_check_failed", f"wire {wire['id']} violates bend radius across a route junction",
                        hint=f"{worst.get('kind')} at {worst.get('at')}: {_mm(worst.get('radius_mm'))} of room, "
                             f"{_mm(wire['min_bend_radius_mm'])} needed by wires {sharing}. Move or re-aim the connector "
                             "whose branch joins there (harness_edit action='edit_connector') so it meets the bundle "
                             f"at a shallower angle, then harness_route(reroute=True, wire_ids={sharing})",
                        wire=wire["id"], segments=wire["segments"], detail=check)
            M.mutated(h)
            return _paged({"ok": True, "harness_id": h["id"], "segments": [s["id"] for s in h["segments"] if s["id"] in made],
                           **({"removed_segments": removed["removed_segments"]} if removed is not None else {}),
                           **({"repathed_segments": repathed, "route_passes": passes} if repathed else {}),
                           "warnings": warnings, "assumptions": M.assumptions(h), **_save(doc_id, h)}, offset, limit, page_offsets)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_check(doc_id: str, harness_id: Optional[str] = None, clearance_mm: Optional[float] = None,
                      ignore_bodies: Optional[list] = None, motion: Optional[dict] = None,
                      offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Check routed geometry, fill, exits, segregation and shield termination without writing a study.

        motion={mate_id,start,stop,steps?} checks conservative component AABBs at sampled poses.
        """
        try:
            h = M.load_harness(doc_id, harness_id)
            M.recompute_derived(h)
            clearance = h["defaults"]["clearance_mm"] if clearance_mm is None else float(clearance_mm)
            if not math.isfinite(clearance) or clearance < 0:
                raise M.HarnessError("bad_clearance", "clearance_mm must be finite and nonnegative")
            issues, results, fills = [], [], []
            integrity = M.validate_harness(h)
            issues.extend({"type": "integrity", "message": x} for x in integrity["errors"])
            by_id = {w["id"]: w for w in h["wires"]}
            for w in h["wires"]:
                if w.get("length_mm") is None:
                    issues.append({"type": "unrouted", "wire": w["id"]})
                if w.get("kind") in ("shielded", "coax") or w.get("signal_class") == "shielded_drain":
                    end = w.get("shield_ground_end")
                    if end not in ("from", "to", "both"):
                        issues.append({"type": "shield_termination", "wire": w["id"], "specified": end,
                                       "basis": "heuristic: single-ended grounding practice; confirm EMC design"})
            for s in h["segments"]:
                members = [by_id[x] for x in s.get("wires", []) if x in by_id]
                if not members:
                    continue
                classes = {w.get("signal_class") for w in members}
                if "power" in classes and classes.intersection({"signal", "data"}):
                    issues.append({"type": "segregation", "segment": s["id"], "wires": s["wires"], "basis": "heuristic: segregate power and low-level signal/data; verify applicable EMC requirements"})
                sleeve = s.get("sleeve_id_mm") or s.get("conduit_id_mm")
                if sleeve:
                    fill = sum(w["insulation_od_mm"] ** 2 for w in members) / float(sleeve) ** 2
                    row = {"segment": s["id"], "area_fraction": fill,
                           "basis": "Sum of insulated circular cross-sectional areas / inside area; not an assembly or compliance assessment."}
                    if s.get("conduit_id_mm") and not s.get("sleeve_id_mm"):
                        row["raceway_reference_limit"] = M.nec_fill_limit(len(members))
                        row["reference_basis"] = "NEC Chapter 9 Table 1 raceway reference; applicability depends on installation."
                        if fill > row["raceway_reference_limit"]:
                            issues.append({"type": "raceway_fill_reference_exceeded", **row})
                    fills.append(row)
                radius = s.get("min_bend_radius_mm")
                if radius is None:
                    issues.append({"type": "bend_not_evaluated", "segment": s["id"], "wires": [w["id"] for w in members if w.get("min_bend_radius_mm") is None]})
                    # Zero is only an RPC query value; its bend verdict is explicitly discarded.
                if s.get("od_mm") is None:
                    issues.append({"type": "bundle_od_not_evaluated", "segment": s["id"], "basis": "Bundle packing is explicitly unknown; supply a packing factor or segment OD."})
                    continue
                check = _check_segment(h, doc_id, s, clearance, h.get("keepouts") or [],
                                       {"ignore_bodies": ignore_bodies} if ignore_bodies is not None else {})
                if radius is None:
                    check.pop("bend_violations", None)
                    check["bend_evaluated"] = False
                else:
                    for item in check.get("bend_violations") or []:
                        issues.append({"type": "bend", "segment": s["id"], **item})
                for item in check.get("clearance_violations") or []:
                    issues.append({"type": "clearance", "segment": s["id"], **item})
                results.append({"segment": s["id"], **check})
            for w in h["wires"]:
                path, junctions = M.wire_route(h, w)
                if not path or w.get("min_bend_radius_mm") is None:
                    continue
                check = _rpc("harness_path_check", {"doc_id": doc_id, "path": path,
                    "od_mm": w["insulation_od_mm"], "min_bend_radius_mm": w["min_bend_radius_mm"],
                    "clearance_mm": clearance, "keepouts": h.get("keepouts") or [],
                    **({"junctions": junctions} if junctions else {}),
                    **({"ignore_bodies": ignore_bodies} if ignore_bodies is not None else {})})
                for item in check.get("bend_violations") or []:
                    issues.append({"type": "wire_bend", "wire": w["id"], **item})
            for c in h["connectors"]:
                for s in h["segments"]:
                    if c["id"] not in (s["a"], s["b"]) or not s.get("wires"):
                        continue
                    p = s["path"] if s["a"] == c["id"] else list(reversed(s["path"]))
                    direction = M.vunit(M.vsub(p[1], p[0]))
                    angle = M.angle_deg(direction, M.connector_exit_dir(c))
                    if angle > 10.0:
                        issues.append({"type": "connector_exit", "connector": c["id"], "segment": s["id"], "angle_deg": angle, "basis": "10 degree alignment screen (heuristic)"})
            motion_result = _motion_check(doc_id, h, motion, clearance) if motion is not None else {"evaluated": False, "basis": "No motion driver supplied."}
            if motion is not None and (not motion_result["evaluated"] or motion_result["conflict_count"]):
                issues.append({"type": "motion_incomplete_or_possible_conflict", "detail": motion_result})
            return _paged({"ok": True, "harness_id": h["id"], "passed": not issues and motion_result["evaluated"], "issues": issues,
                          "issue_count": len(issues), "segments": results, "motion": motion_result,
                          "fill": fills, "assumptions": M.assumptions(h),
                          "warnings": integrity["warnings"] + ["Bundle OD is an estimate, not a verified enclosing diameter; twisted groups require their actual assembled OD."],
                          "limitations": ["Static clearance is based on engine tessellation.", "Motion between sampled poses is not checked.", "An omitted motion driver leaves the overall check incomplete."]}, offset, limit, page_offsets)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_build_geometry(doc_id: str, harness_id: Optional[str] = None, offset: int = 0,
                               limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Build separate sweep bodies in a part or a persistent harness component in an assembly."""
        try:
            from services.mcp.sketch3d_tools import _apply_ops, _curves_to_entities, _profile_plane_ops
            h = M.load_harness(doc_id, harness_id)
            M.recompute_derived(h)
            tree = _rpc("doc_tree", {"doc_id": doc_id}).get("tree") or {}
            assembly = tree.get("schema") == "aicad.asm/1"
            active = [s for s in h["segments"] if s.get("wires")]
            geometry_warnings = (["Ribbon geometry is a circumscribed circular envelope, not a rectangular ribbon; orientation and twists are not modeled."]
                                 if any(w.get("kind") == "ribbon" for w in h["wires"]) else [])
            if not active:
                raise M.HarnessError("nothing_routed", "no routed bundle segments to build")
            if any(s.get("od_mm") is None for s in active):
                raise M.HarnessError("bundle_od_required", "a routed bundle has no calculated OD", hint="Supply a packing factor through harness_route before building geometry.")
            signature = hashlib.sha256(json.dumps([(s["id"], s["path"], s["od_mm"]) for s in active], sort_keys=True).encode()).hexdigest()
            old = h.get("geometry") or {}
            features = {f.get("id") for f in tree.get("features", [])}
            components = {c.get("id") for c in tree.get("components", [])}
            if assembly:
                if old.get("component_id") and old["component_id"] not in components:
                    raise M.HarnessError("geometry_ownership_incomplete", "previous harness component is missing; refusing a potentially duplicating rebuild")
                if old.get("signature") == signature and old.get("component_id") in components and Path(old.get("path", "")).is_file():
                    return _paged({"ok": True, "harness_id": h["id"], "unchanged": True, "geometry": old,
                                   "warnings": geometry_warnings}, offset, limit, page_offsets)
                ops = []
            else:
                if old.get("signature") == signature and old.get("feature_ids") and set(old["feature_ids"]) <= features:
                    return _paged({"ok": True, "harness_id": h["id"], "unchanged": True, "geometry": old,
                                   "warnings": geometry_warnings}, offset, limit, page_offsets)
                if old.get("feature_ids") and not set(old["feature_ids"]) <= features:
                    raise M.HarnessError("geometry_ownership_incomplete", "some previously built harness features are missing; refusing a potentially duplicating rebuild", missing=sorted(set(old["feature_ids"]) - features))
                ops = [{"op": "delete_feature", "id": fid} for fid in reversed(old.get("feature_ids") or []) if fid in features]
            indexes = []
            for s in active:
                pts = s["path"]
                base = len(ops)
                label = f"Harness_{h['id']}_{s['id']}"
                ops.append({"op": "add_feature", "feature": {"type": "sketch3d", "name": label + "_path", "params": {"entities": _curves_to_entities([{"kind": "polyline", "pts": pts}]), "constraints": []}}})
                plane, uv, _ = _profile_plane_ops(pts[0], M.vunit(M.vsub(pts[1], pts[0])))
                plane["feature"]["name"] = label + "_plane"
                ops.append(plane)
                ops.append({"op": "add_feature", "feature": {"type": "sketch", "name": label + "_profile", "params": {"entities": [{"id": "e1", "type": "circle", "construction": False, "center": uv, "radius": s["od_mm"] / 2}], "constraints": []}, "refs": {"plane": f"${base+1}"}}})
                ops.append({"op": "add_feature", "feature": {"type": "sweep", "name": label, "params": {"mode": "new", "merge": False}, "refs": {"profile": [{"kind": "region", "sketch": f"${base+2}"}], "path": f"${base}"}}})
                indexes.extend(range(base, base+4))
            if assembly:
                import uuid
                target = _sanitize_output_path(f"{h['id']}_harness_{uuid.uuid4().hex}.step+")
                if Path(target).exists():
                    raise M.HarnessError("geometry_path_exists", "refusing to overwrite an existing companion part", path=target)
                private = _rpc("doc_new", {"kind": "part", "name": f"Harness_{h['id']}", "internal": True, "resume_if_open": False}).get("doc_id")
                if not private:
                    raise M.HarnessError("geometry_build_failed", "could not create internal harness part")
                try:
                    built = _rpc("doc_apply_ops", {"doc_id": private, "ops": ops})
                    failures = [s for s in (built.get("rebuild") or {}).get("feature_status", []) if not s.get("ok")]
                    if failures:
                        raise M.HarnessError("geometry_build_failed", "harness part sweep failed", failures=failures)
                    _rpc("doc_save", {"doc_id": private, "path": target, "preserve_path": True})
                finally:
                    cb.call("doc_close", {"doc_id": private})
                if not Path(target).is_file():
                    raise M.HarnessError("geometry_save_failed", "engine did not create the companion part", path=target)
                assembly_ops = []
                if old.get("component_id"):
                    assembly_ops.append({"op": "asm_delete_component", "id": old["component_id"]})
                index = len(assembly_ops)
                assembly_ops.append({"op": "asm_insert_component", "source": {"path": target},
                                     "name": f"Harness_{h['id']}", "fixed": True})
                try:
                    applied = _rpc("doc_apply_ops", {"doc_id": doc_id, "ops": assembly_ops})
                except Exception as e:
                    raise M.HarnessError("assembly_insert_failed", f"companion part was saved but assembly insertion failed: {e}", path=target) from e
                component_id = (applied.get("id_map") or {}).get(f"${index}")
                if not component_id:
                    raise M.HarnessError("geometry_ids_missing", "assembly operation did not report the inserted component id", path=target, detail=applied)
                h["geometry"] = {"signature": signature, "component_id": component_id, "path": target}
            else:
                applied = _apply_ops(doc_id, ops)
                ids = [(applied.get("id_map") or {}).get(f"${i}") for i in indexes]
                if any(not fid for fid in ids):
                    raise M.HarnessError("geometry_ids_missing", "geometry batch was applied but engine did not return all feature IDs", detail=applied)
                h["geometry"] = {"signature": signature, "feature_ids": ids, "sweep_ids": ids[3::4]}
            try:
                saved = _save(doc_id, h)
            except Exception as e:
                raise M.HarnessError("geometry_not_recorded", f"geometry was built but harness study was not saved: {e}", geometry=h["geometry"], hint="Do not rebuild until these features are reconciled.") from e
            return _paged({"ok": True, "harness_id": h["id"], "geometry": h["geometry"],
                           "warnings": geometry_warnings, **saved}, offset, limit, page_offsets)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_report(doc_id: str, harness_id: Optional[str] = None, offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Read cut list, wire lengths, BOM and connector pin/from-to tables; lists are paginated."""
        try:
            h = M.load_harness(doc_id, harness_id)
            M.recompute_derived(h)
            report = M.build_report(h)
            return _paged({"ok": True, "harness_id": h["id"], "report": report}, offset, limit, page_offsets)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_formboard(doc_id: str, harness_id: Optional[str] = None, format: str = "svg",
                          path: Optional[str] = None, scale: Optional[float] = None,
                          offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Write an SVG, DXF or PDF true-length formboard; reports overlap and length verification."""
        try:
            h = M.load_harness(doc_id, harness_id)
            fmt = str(format).lower()
            if fmt not in ("svg", "dxf", "pdf"):
                raise M.HarnessError("bad_format", "format must be svg, dxf or pdf")
            target = _sanitize_output_path(path or f"{h['id']}_formboard.{fmt}")
            if Path(target).suffix.lower() != "." + fmt:
                raise M.HarnessError("extension_mismatch", f"output path must end in .{fmt}")
            layout = F.layout_formboard(h, scale)
            if fmt == "svg":
                Path(target).write_text(F.render_svg(layout), encoding="utf-8")
            elif fmt == "dxf":
                F.write_dxf(layout, target)
            else:
                F.write_pdf(layout, target)
            return _paged({"ok": layout["ok"], "harness_id": h["id"], "path": target, "format": fmt,
                          "layout_ok": layout["ok"], "overlaps": layout["overlaps"],
                          "unrouted_wires": layout["unrouted_wires"],
                          "verification": layout["verification"], "scale": layout["scale"],
                          "warnings": layout["warnings"], "drawing_sheet": False}, offset, limit, page_offsets)
        except Exception as e:
            return _error(e)

    @mcp.tool()
    def harness_edit(doc_id: str, harness_id: str, action: str, wire_id: Optional[str] = None,
                     min_bend_radius_mm: Optional[float] = None, service_loop_mm: Optional[float] = None,
                     strip_mm: Optional[float] = None, signal_class: Optional[str] = None,
                     shield_ground_end: Optional[str] = None, packing_factor: Optional[float] = None,
                     segment_id: Optional[str] = None, distance_mm: Optional[float] = None,
                     point: Optional[list] = None, insulation: Optional[str] = None,
                     segment_od_mm: Optional[float] = None,
                     segment_min_bend_radius_mm: Optional[float] = None,
                     segment_packing_factor: Optional[float] = None,
                     clear_segment_overrides: Optional[list] = None,
                     connector_id: Optional[str] = None, frame: Optional[dict] = None,
                     exit_dir: Optional[list] = None) -> str:
        """Edit wires, defaults, segments, clamps and connectors; remove wires, segments or connectors; delete a harness.

        Actions: add_clamp, edit_segment, edit_wire, remove_wire, remove_segment, edit_connector,
        remove_connector, set_packing_factor, delete_harness.

        Segment radii are centerline radii. Clear selected overrides by naming
        od_mm, min_bend_radius_mm or packing_factor in clear_segment_overrides.
        remove_wire and remove_segment unroute the affected wires and prune segments no wire
        carries. edit_connector (connector_id plus any of frame{origin,normal,up} or exit_dir)
        removes the segments touching the connector and reports unrouted_wires; re-run
        harness_route for them. remove_connector needs a connector no wire references
        (connector_in_use lists the wires). delete_harness deletes the study; built geometry stays.
        """
        try:
            h = M.load_harness(doc_id, harness_id)
            if action == "add_clamp":
                if not segment_id:
                    raise M.HarnessError("segment_required", "segment_id is required for add_clamp")
                if (distance_mm is None) == (point is None):
                    raise M.HarnessError("clamp_position_required", "supply exactly one of distance_mm or point")
                seg = M.get_segment(h, segment_id)
                path = seg["path"]
                if distance_mm is not None:
                    distance = float(distance_mm)
                    length = M.seg_length(seg)
                    if not math.isfinite(distance) or not 0 < distance < length:
                        raise M.HarnessError("bad_clamp_distance", "distance_mm must be strictly inside the segment",
                                             length_mm=length)
                    pos, _ = M.point_at_distance(path, distance)
                else:
                    requested = M.v3(point, "point")
                    gap, _, _, pos = M.closest_on_polyline(requested, path)
                    if gap > 1e-3:
                        raise M.HarnessError("clamp_point_off_path", "point must lie within 0.001 mm of the segment path",
                                             distance_mm=gap)
                if M.vdist(pos, path[0]) <= 1e-3 or M.vdist(pos, path[-1]) <= 1e-3:
                    raise M.HarnessError("clamp_at_endpoint", "clamp must be more than 0.001 mm from either endpoint")
                node_id = M.split_segment_at(h, segment_id, pos, kind="clamp")
                node = M.get_node(h, node_id)
                if not node or node.get("kind") != "clamp":
                    raise M.HarnessError("clamp_split_failed", "segment did not split at an interior point")
                M.mutated(h)
                result = {"clamp": node, "segments": [s["id"] for s in h["segments"]
                                                        if node_id in (s["a"], s["b"])]}
            elif action == "set_packing_factor":
                if packing_factor is None or not math.isfinite(float(packing_factor)) or not 0 < float(packing_factor) <= 1:
                    raise M.HarnessError("bad_packing_factor", "packing_factor must be finite and in (0, 1]")
                h["defaults"]["packing_factor"] = float(packing_factor)
                h["packing_factor_basis"] = "caller supplied"
                M.mutated(h)
                result = {"packing_factor": float(packing_factor),
                          "warning": "Existing routes must be revalidated with harness_check or harness_route."}
            elif action == "edit_segment":
                if not segment_id:
                    raise M.HarnessError("segment_required", "segment_id is required for edit_segment")
                segment = M.get_segment(h, segment_id)
                values = {"od_mm": segment_od_mm, "min_bend_radius_mm": segment_min_bend_radius_mm,
                          "packing_factor": segment_packing_factor}
                clears = clear_segment_overrides if clear_segment_overrides is not None else []
                if not isinstance(clears, list) or any(k not in values for k in clears):
                    raise M.HarnessError("bad_segment_override", "clear_segment_overrides must list od_mm, min_bend_radius_mm or packing_factor")
                if any(values[k] is not None for k in clears):
                    raise M.HarnessError("bad_segment_override", "cannot set and clear the same override")
                if not clears and all(v is None for v in values.values()):
                    raise M.HarnessError("bad_segment_override", "supply a segment override to set or clear")
                for key in clears:
                    segment.pop("override_" + key, None)
                for key, value in values.items():
                    if value is not None:
                        number = float(value)
                        if not math.isfinite(number) or number <= 0 or (key == "packing_factor" and number > 1):
                            raise M.HarnessError("bad_segment_override", f"invalid {key}: {value}")
                        segment["override_" + key] = number
                M.mutated(h)
                M.recompute_derived(h)
                result = {"segment": segment, "warning": "Existing routes must be revalidated with harness_check or harness_route."}
            elif action == "remove_wire":
                if not wire_id:
                    raise M.HarnessError("wire_required", "wire_id is required")
                result = {"removed_wire": wire_id, **M.remove_wire(h, wire_id)}
            elif action == "remove_segment":
                if not segment_id:
                    raise M.HarnessError("segment_required", "segment_id is required for remove_segment")
                M.get_segment(h, segment_id)
                result = {"removed_segment": segment_id, **M.remove_segments(h, [segment_id])}
                M.mutated(h)
            elif action == "edit_connector":
                if not connector_id:
                    raise M.HarnessError("connector_required", "connector_id is required for edit_connector")
                conn, report = M.edit_connector(h, connector_id, frame=frame, exit_dir=exit_dir)
                result = {"connector": {k: v for k, v in conn.items() if k != "cavities"}, **report}
                if report["unrouted_wires"]:
                    result["warning"] = "These wires are unrouted: re-run harness_route for them."
            elif action == "remove_connector":
                if not connector_id:
                    raise M.HarnessError("connector_required", "connector_id is required for remove_connector")
                result = {"removed_connector": M.get_connector(h, connector_id)["id"],
                          **M.remove_connector(h, connector_id)}
            elif action == "delete_harness":
                if not M.delete_harness(doc_id, h["id"]):
                    raise M.HarnessError("unknown_harness", f"no harness {h['id']!r} in document {doc_id}")
                deleted = {"ok": True, "harness_id": h["id"], "deleted_harness": h["id"], "doc_id": doc_id}
                if h.get("geometry"):
                    deleted["warning"] = "Geometry built from this harness is still in the document; delete it separately."
                return dumps(deleted)
            elif action == "edit_wire":
                if not wire_id:
                    raise M.HarnessError("wire_required", "wire_id is required")
                w = M.get_wire(h, wire_id)
                for key, val in (("min_bend_radius_mm", min_bend_radius_mm), ("service_loop_mm", service_loop_mm), ("strip_mm", strip_mm)):
                    if val is not None:
                        num = float(val)
                        if not math.isfinite(num) or num < 0 or (key == "min_bend_radius_mm" and num == 0):
                            raise M.HarnessError("bad_value", f"invalid {key}: {val}")
                        w[key] = num
                        if key == "min_bend_radius_mm":
                            w["bend_radius_basis"] = "explicit (caller supplied)"
                            w["bend_radius_confidence"] = "explicit"
                if signal_class is not None:
                    normalized_class = str(signal_class).strip().lower().replace("-", "_")
                    if normalized_class not in M.SIGNAL_CLASSES:
                        raise M.HarnessError("bad_signal_class", f"signal_class must be one of {M.SIGNAL_CLASSES}")
                    w["signal_class"] = normalized_class
                if shield_ground_end is not None:
                    if shield_ground_end not in M.SHIELD_ENDS or (w.get("kind") not in ("shielded", "coax") and w.get("signal_class") != "shielded_drain"):
                        raise M.HarnessError("bad_shield_end", "shield grounding requires a shielded cable or drain and a valid end")
                    w["shield_ground_end"] = shield_ground_end
                if insulation is not None:
                    w["insulation"] = M.validate_insulation(insulation)
                    if min_bend_radius_mm is None and w.get("bend_radius_confidence") == "low":
                        w["min_bend_radius_mm"] = None
                M.mutated(h)
                result = {"wire": w}
            else:
                raise M.HarnessError("bad_action", "action must be add_clamp, delete_harness, edit_connector, edit_segment, edit_wire, remove_connector, remove_segment, remove_wire or set_packing_factor")
            return dumps({"ok": True, "harness_id": h["id"], **result, **_save(doc_id, h)})
        except Exception as e:
            return _error(e)
