"""Document-persisted harness authoring, routing, checks and manufacturing deliverables.

The routing/check RPCs operate on tessellated document solids; their clearance is a
surface gap. Connector frames and cable specifications are explicit engineering inputs.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Optional

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
    node = M.get_node(h, node_id)
    out = {"pos": node["pos"]}
    if node.get("kind") == "connector":
        out["dir"] = M.connector_exit_dir(M.get_connector(h, node_id))
    return out


def _nearest_component_point(h: dict, component: int, target: list, comps: dict) -> tuple:
    candidates = [(M.vdist(n["pos"], target), "node", n["id"], n["pos"])
                  for n in h["nodes"] if comps.get(n["id"]) == component]
    for seg in h["segments"]:
        if comps.get(seg["a"]) == component:
            dist, _i, _t, point = M.closest_on_polyline(target, seg["path"])
            candidates.append((dist, "segment", seg["id"], point))
    return min(candidates, key=lambda v: v[0])


def _resolve_candidate(h: dict, cand: tuple) -> str:
    return cand[2] if cand[1] == "node" else M.split_segment_at(h, cand[2], cand[3])


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
                      packing_factor: Optional[float] = None, offset: int = 0, limit: int = 100, page_offsets: Optional[dict] = None) -> str:
        """Route selected wires through the engine, reusing the existing forest and splitting breakouts."""
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
            added, warnings = [], ["Bundle OD is an estimate, not a verified enclosing diameter; confirm the physical bundle. Twisted groups require their actual assembled OD."]
            for w in selected:
                a, b = w["from"]["connector"], w["to"]["connector"]
                comps = M.components(h)
                if comps[a] == comps[b]:
                    continue
                # Attach the smaller gap between the two trees, including segment interiors.
                best = None
                for node in h["nodes"]:
                    if comps.get(node["id"]) != comps[a]:
                        continue
                    other = _nearest_component_point(h, comps[b], node["pos"], comps)
                    candidate = (other[0], node["id"], other)
                    if best is None or candidate[0] < best[0]:
                        best = candidate
                start_id = best[1]
                end_id = _resolve_candidate(h, best[2])
                # Conservative diameter/radius for every wire that can use the joining path.
                active = [x for x in h["wires"] if {comps[x["from"]["connector"]], comps[x["to"]["connector"]]} == {comps[a], comps[b]}]
                if len(active) >= 3 and h["defaults"]["packing_factor"] is None:
                    raise M.HarnessError("packing_factor_required", "bundle packing factor is explicitly unknown",
                                         hint="Supply packing_factor for this route or edit the harness default.")
                od = M.bundle_od_mm([x["insulation_od_mm"] for x in active], h["defaults"]["packing_factor"])
                radius, _, _ = M.bundle_bend_radius(active, od)
                params = {"doc_id": doc_id, "start": _route_endpoint(h, start_id), "end": _route_endpoint(h, end_id),
                          "od_mm": od, "min_bend_radius_mm": radius, "clearance_mm": clearance,
                          "keepouts": keepouts if keepouts is not None else h.get("keepouts") or [],
                          "prefer_paths": [s["path"] for s in h["segments"]]}
                for key, val in (("ignore_bodies", ignore_bodies), ("grid_mm", grid_mm), ("max_nodes", max_nodes), ("endpoint_relax_mm", endpoint_relax_mm)):
                    if val is not None:
                        params[key] = val
                routed = _rpc("harness_route", params)
                path = routed.get("path") or []
                if len(path) < 2:
                    raise M.HarnessError("bad_engine_reply", "router returned no usable path")
                seg = M.add_segment(h, start_id, end_id, path)
                added.append(seg["id"])
                warnings.extend(routed.get("warnings") or [])
                M.recompute_derived(h)
            # Recheck the final enlarged bundles, not just the newly routed centerlines.
            for seg in h["segments"]:
                if not seg.get("wires"):
                    continue
                if seg.get("od_mm") is None:
                    raise M.HarnessError("packing_factor_required", f"bundle {seg['id']} has no usable outer diameter",
                                         hint="Supply a packing factor or a segment OD override.")
                check = _rpc("harness_path_check", {"doc_id": doc_id, "path": seg["path"], "od_mm": seg["od_mm"],
                    "min_bend_radius_mm": seg["min_bend_radius_mm"], "clearance_mm": clearance,
                    "keepouts": keepouts if keepouts is not None else h.get("keepouts") or [],
                    **({"ignore_bodies": ignore_bodies} if ignore_bodies is not None else {})})
                if check.get("bend_violations") or check.get("clearance_violations"):
                    raise M.HarnessError("route_check_failed", f"final bundle {seg['id']} violates bend or clearance", result=check)
            # Check full wire paths too: a breakout can introduce a turn between segments.
            for wire in h["wires"]:
                path = M.wire_route_points(h, wire)
                if not path:
                    continue
                check = _rpc("harness_path_check", {"doc_id": doc_id, "path": path,
                    "od_mm": wire["insulation_od_mm"], "min_bend_radius_mm": wire["min_bend_radius_mm"],
                    "clearance_mm": clearance, "keepouts": keepouts if keepouts is not None else h.get("keepouts") or [],
                    **({"ignore_bodies": ignore_bodies} if ignore_bodies is not None else {})})
                if check.get("bend_violations"):
                    raise M.HarnessError("route_check_failed", f"wire {wire['id']} violates bend radius across a route junction", result=check)
            M.mutated(h)
            return _paged({"ok": True, "harness_id": h["id"], "segments": added, "warnings": warnings,
                           "assumptions": M.assumptions(h), **_save(doc_id, h)}, offset, limit, page_offsets)
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
                check = _rpc("harness_path_check", {"doc_id": doc_id, "path": s["path"], "od_mm": s["od_mm"],
                    "min_bend_radius_mm": radius if radius is not None else 0.0, "clearance_mm": clearance,
                    "keepouts": h.get("keepouts") or [],
                    **({"ignore_bodies": ignore_bodies} if ignore_bodies is not None else {})})
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
                path = M.wire_route_points(h, w)
                if not path or w.get("min_bend_radius_mm") is None:
                    continue
                check = _rpc("harness_path_check", {"doc_id": doc_id, "path": path,
                    "od_mm": w["insulation_od_mm"], "min_bend_radius_mm": w["min_bend_radius_mm"],
                    "clearance_mm": clearance, "keepouts": h.get("keepouts") or [],
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
                     clear_segment_overrides: Optional[list] = None) -> str:
        """Edit wires/defaults, edit_segment overrides, or add_clamp on a segment.

        Segment radii are centerline radii. Clear selected overrides by naming
        od_mm, min_bend_radius_mm or packing_factor in clear_segment_overrides.
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
                M.remove_wire(h, wire_id)
                result = {"removed_wire": wire_id}
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
                raise M.HarnessError("bad_action", "action must be add_clamp, edit_segment, edit_wire, remove_wire or set_packing_factor")
            return dumps({"ok": True, "harness_id": h["id"], **result, **_save(doc_id, h)})
        except Exception as e:
            return _error(e)
