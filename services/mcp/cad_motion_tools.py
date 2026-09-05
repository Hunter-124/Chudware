"""CAD kinematic-evidence MCP tools: sweep_envelope, steering_turning_radius,
motion_fea_handoff_sweep.

Every tool here is a COMPOSITION over the primitives that already exist —
``simulate_motion`` and ``motion_fea_handoff`` (services/mcp/cad_asm_tools.py) — resolved
out of the shared FastMCP registry at call time, so their validation, dialect handling and
dispatch guard run exactly once and are never re-implemented here. What this module adds is
the mechanism-level arithmetic a rover/linkage claim needs: a swept ENVELOPE (and optionally
the swept SOLID), a closed-form Ackermann TURNING RADIUS, and a batched motion→FEA sweep
across several mechanism positions.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import json
import math

from .cad_common import (
    _doc,
    _dumps,
    _err,
    _normalize_optional_slot,
    _publish_current,
    _solve_sync_or_handoff,
    _start_job,
    _tmp,
    cb,
    coerce_json_or_repr,
    note_touched_doc,
    require_list,
)

#: A wheel that turns by less than this between the sweep extremes is not "steered".
_STEER_EPS_DEG = 0.05

#: Same-axle wheel recognition tolerances (see steering_turning_radius' `assumptions`).
_WHEEL_SIZE_TOL = 0.25
_WHEEL_Z_TOL = 0.25


def register_cad_motion_tools(mcp: FastMCP) -> None:
    """CAD kinematic-evidence MCP tools: sweep_envelope, steering_turning_radius, motion_fea_handoff_sweep."""

    def _tool(name: str):
        """The sibling MCP tool's registered callable (guard wrapper included).

        Resolved lazily: this module registers alongside cad_asm_tools on the SAME
        FastMCP instance, and registration order must not become a load-order rule.
        """
        entries = getattr(getattr(mcp, "_tool_manager", None), "_tools", None) or {}
        entry = entries.get(name)
        fn = getattr(entry, "fn", None)
        if fn is None:
            raise ValueError(
                f"{name} is not registered on this server; the motion tools compose it "
                "and cannot substitute their own copy.")
        return fn

    def _call_tool(name: str, **kwargs) -> dict:
        """Call a sibling tool and decode its JSON reply (tools return text)."""
        raw = _tool(name)(**kwargs)
        if isinstance(raw, (dict, list)):
            return raw if isinstance(raw, dict) else {"ok": True, "result": raw}
        try:
            out = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{name} returned a non-JSON reply: {raw!r:.200}") from exc
        if not isinstance(out, dict):
            raise ValueError(f"{name} returned {type(out).__name__}, expected an object")
        return out

    # ------------------------------------------------------------------ geometry helpers

    def _components(doc_id: str) -> list:
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        comps = tr.get("components") if isinstance(tr, dict) else None
        return [c for c in (comps or []) if isinstance(c, dict) and not c.get("suppressed")]

    def _resolve_components(doc_id: str, wanted) -> list:
        """`wanted` (ids and/or names, or None = all) -> the component dicts, in doc order."""
        comps = _components(doc_id)
        if not comps:
            raise ValueError("document has no components (open/build an assembly first)")
        if wanted is None:
            return comps
        wanted = require_list(wanted, "components", 'a list of component ids or names')
        out = []
        for token in wanted:
            key = str(token).strip()
            hit = next((c for c in comps
                        if c.get("id") == key or (c.get("name") or "") == key), None)
            if hit is None:
                known = ", ".join(f"{c.get('id')}({c.get('name')})" for c in comps)
                raise ValueError(f"component '{key}' not found. Components: {known}")
            if hit not in out:
                out.append(hit)
        return out

    _LOCAL_BBOX: dict = {}

    def _local_bbox(source_path: str) -> dict:
        """{min,max} of a component's own part file, in its LOCAL frame (cached per path)."""
        hit = _LOCAL_BBOX.get(source_path)
        if hit is not None:
            return hit
        part_doc = cb.call("doc_open", {"path": source_path}).get("doc_id")
        if not part_doc:
            raise ValueError(f"component source '{source_path}' could not be opened")
        bb = cb.call("doc_bbox", {"doc_id": part_doc}) or {}
        lo, hi = bb.get("min"), bb.get("max")
        if not (isinstance(lo, list) and isinstance(hi, list) and
                len(lo) == 3 and len(hi) == 3):
            raise ValueError(f"component source '{source_path}' has no bounding box "
                             "(it builds no solid)")
        hit = {"min": [float(v) for v in lo], "max": [float(v) for v in hi],
               "doc_id": part_doc}
        _LOCAL_BBOX[source_path] = hit
        return hit

    def _source_path(comp: dict) -> str:
        path = ((comp.get("source") or {}).get("path")) or ""
        if not path:
            raise ValueError(f"component '{comp.get('id')}' has no source part file; "
                             "its geometry cannot be swept")
        return path

    def _xform_point(m: list, p) -> list:
        """Row-major 4x4 (flat 16, the simulate_motion tool's public shape) applied to p."""
        return [m[0] * p[0] + m[1] * p[1] + m[2] * p[2] + m[3],
                m[4] * p[0] + m[5] * p[1] + m[6] * p[2] + m[7],
                m[8] * p[0] + m[9] * p[1] + m[10] * p[2] + m[11]]

    def _corners(bbox: dict) -> list:
        lo, hi = bbox["min"], bbox["max"]
        return [[lo[0] if not i & 1 else hi[0],
                 lo[1] if not i & 2 else hi[1],
                 lo[2] if not i & 4 else hi[2]] for i in range(8)]

    def _bbox_of(points: list) -> dict:
        lo = [min(p[a] for p in points) for a in range(3)]
        hi = [max(p[a] for p in points) for a in range(3)]
        return {"min": lo, "max": hi,
                "size": [hi[a] - lo[a] for a in range(3)],
                "center": [(hi[a] + lo[a]) / 2.0 for a in range(3)]}

    def _finite16(m, label: str) -> list:
        if not isinstance(m, list) or len(m) != 16:
            raise ValueError(f"{label} is not a 4x4 transform")
        out = [float(v) for v in m]
        if not all(math.isfinite(v) for v in out):
            raise ValueError(f"{label} is non-finite")
        return out

    def _frame_transforms(frame: dict) -> dict:
        """{component_id: row-major 16} for the top-level components of one sweep frame."""
        out: dict = {}
        for body in frame.get("bodies") or []:
            cid = str(body.get("component") or "")
            if not cid or "#" in cid or cid in out:
                continue
            out[cid] = _finite16(body.get("transform"), f"transform of '{cid}'")
        return out

    def _sweep(doc_id: str, mate_id, start, stop, steps: int,
               dynamics: Optional[dict] = None, check_interference: bool = False) -> dict:
        """One simulate_motion run through the registered tool (its validation, its dialect)."""
        out = _call_tool("simulate_motion", mate_id=mate_id, start=start, stop=stop,
                         steps=int(steps), check_interference=bool(check_interference),
                         dynamics=dynamics, doc_id=doc_id)
        if not out.get("ok"):
            raise ValueError("motion sweep did not converge: " +
                             str(out.get("error") or out.get("summary") or "unknown"))
        if not (out.get("trajectory") or []):
            raise ValueError("motion sweep returned no trajectory")
        return out

    # ------------------------------------------------------------------ swept solid

    def _rigid_params(m: list) -> dict:
        """Rotation (axis + degrees about the origin) and translation of a rigid 4x4.

        `transform` takes rotate-about-pivot then translate, which is exactly this
        decomposition with pivot at the origin — an exact representation of any rigid pose,
        so the fused sweep is the real placed geometry, not an approximation.
        """
        r = [[m[0], m[1], m[2]], [m[4], m[5], m[6]], [m[8], m[9], m[10]]]
        trace = r[0][0] + r[1][1] + r[2][2]
        cos_a = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
        angle = math.acos(cos_a)
        if angle < 1e-9:
            axis = [0.0, 0.0, 1.0]
            angle = 0.0
        elif math.pi - angle < 1e-6:
            # Near 180 deg the skew part vanishes; take the axis from R + I.
            diag = [max(0.0, (r[a][a] + 1.0) / 2.0) for a in range(3)]
            k = max(range(3), key=lambda a: diag[a])
            axis = [0.0, 0.0, 0.0]
            axis[k] = math.sqrt(diag[k])
            for a in range(3):
                if a != k:
                    axis[a] = (r[k][a] + r[a][k]) / (4.0 * axis[k])
        else:
            s = 2.0 * math.sin(angle)
            axis = [(r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s,
                    (r[1][0] - r[0][1]) / s]
        norm = math.sqrt(sum(a * a for a in axis)) or 1.0
        axis = [a / norm for a in axis]
        return {"rotate": axis, "angle": math.degrees(angle),
                "translate": [m[3], m[7], m[11]]}

    def _build_swept_solid(name: str, source_path: str, transforms: list) -> dict:
        """Fuse one placed copy per swept step into a single `<component>_sweep` body.

        The component's part is exported once and imported once; each step is a
        `transform` feature with `copy:true` off that import (the LAST one consumes the
        import so no un-swept body is left behind), and the copies are fused pairwise with
        the `boolean` feature `op:"fuse"` (contracts/feature_params.md "Multi-body").
        """
        step_file = _tmp(".step")
        src_doc = _local_bbox(source_path)["doc_id"]
        cb.call("doc_export_step", {"doc_id": src_doc, "path": step_file})
        body_name = f"{name}_sweep"
        doc = cb.call("doc_new", {"kind": "part", "name": body_name}).get("doc_id")
        if not doc:
            raise ValueError("doc_new returned no doc_id for the swept-volume part")
        note_touched_doc(doc)

        ops = [{"op": "add_feature", "feature": {
            "type": "import", "name": f"{name}_src",
            "params": {"source_path": step_file, "format": "step",
                       "original_name": name}}}]
        placed = []
        last = len(transforms) - 1
        for i, m in enumerate(transforms):
            rp = _rigid_params(m)
            ops.append({"op": "add_feature", "feature": {
                "type": "transform", "name": f"{name}_pose{i}",
                "params": {"body": "$0", "translate": rp["translate"],
                           "rotate": rp["rotate"], "angle": rp["angle"],
                           # The final pose CONSUMES the imported body, so the part holds
                           # only swept copies — an extra un-posed body would corrupt the
                           # fused volume.
                           "copy": i != last}}})
            placed.append(f"${len(ops) - 1}")
        acc = placed[0]
        for i, other in enumerate(placed[1:]):
            final = i == len(placed) - 2
            ops.append({"op": "add_feature", "feature": {
                "type": "boolean",
                "name": body_name if final else f"{name}_sweep{i}",
                "params": {"op": "fuse", "a": acc, "b": other}}})
            acc = f"${len(ops) - 1}"
        r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
        failed = [s for s in ((r.get("rebuild") or {}).get("feature_status") or [])
                  if not s.get("ok")]
        _publish_current(doc)
        out = {"doc_id": doc, "body_name": body_name,
               "steps_fused": len(transforms), "source_step": step_file}
        if failed:
            out["ok"] = False
            out["failed_features"] = [{"id": s.get("id"), "error": s.get("error")}
                                      for s in failed]
            return out
        mass = cb.call("eval_mass", {"doc_id": doc}) or {}
        bb = cb.call("doc_bbox", {"doc_id": doc}) or {}
        out.update({"ok": True,
                    "volume_mm3": mass.get("volume_mm3"),
                    "body_count": mass.get("body_count"),
                    "bbox": {"min": bb.get("min"), "max": bb.get("max"),
                             "size": bb.get("size")}})
        return out

    # ------------------------------------------------------------------ sweep_envelope

    @mcp.tool()
    def sweep_envelope(mate_id: Optional[str] = None, start: Optional[float] = None,
                       stop: Optional[float] = None, steps: int = 24,
                       components: Optional[list] = None, fuse: bool = False,
                       doc_id: Optional[str] = None) -> str:
        """SWEPT ENVELOPE of a moving mechanism — the volume its parts need to be given.

        Drives one dimensional mate over [start, stop] with `simulate_motion` and, at every
        sample, transforms each listed component's own bounding box (all 8 corners, so a
        rotated pose is not under-reported) into world coordinates. Returns the union
        envelope over the whole sweep plus the per-step, per-component boxes — the numbers a
        clearance claim ("the wheel never leaves its arch") is actually made from.

        components : component ids or names to sweep (default: every non-suppressed
                     component). Each needs a source part file.
        fuse       : also BUILD the swept solid. Each component's part is imported into a new
                     part document and one `transform` copy per step is fused
                     (`boolean op:"fuse"`) into a body named `<component>_sweep`; the reply
                     carries that document's id, volume_mm3 and bbox. Costs one boolean per
                     step — keep `steps` modest (<= ~24) when fusing. Default False (bbox
                     envelope only, no geometry written).

        Returns {ok, doc_id, mate_id, driver, components, envelope:{min,max,size,center},
                 per_component:{id:{min,max,size,center}}, per_step:[{i,value,
                 components:{id:{min,max,size}}}], swept_solids:[{component, doc_id,
                 body_name, volume_mm3, bbox}] (fuse only), warnings}. The envelope is
        AXIS-ALIGNED in world coordinates.
        """
        try:
            d = _doc(doc_id)
            mate_id = _normalize_optional_slot(mate_id)
            comps = _resolve_components(d, _normalize_optional_slot(components))
            steps = int(steps)
            if steps < 2:
                raise ValueError("sweep_envelope: `steps` must be >= 2.")
            if fuse and steps > 64:
                raise ValueError("sweep_envelope: fuse=True with more than 64 steps is one "
                                 "boolean per step — reduce `steps`.")
            local = {c["id"]: _local_bbox(_source_path(c)) for c in comps}

            out = _sweep(d, mate_id, start, stop, steps)
            trajectory = out.get("trajectory") or []
            warnings = []

            per_step = []
            all_points: list = []
            per_component_points: dict = {cid: [] for cid in local}
            poses: dict = {cid: [] for cid in local}
            for frame in trajectory:
                xf = _frame_transforms(frame)
                row: dict = {}
                for cid, bbox in local.items():
                    m = xf.get(cid)
                    if m is None:
                        continue
                    pts = [_xform_point(m, p) for p in _corners(bbox)]
                    per_component_points[cid].extend(pts)
                    all_points.extend(pts)
                    poses[cid].append(m)
                    box = _bbox_of(pts)
                    row[cid] = {"min": box["min"], "max": box["max"], "size": box["size"]}
                if not row:
                    warnings.append(
                        f"step {frame.get('i')} placed none of the requested components")
                per_step.append({"i": frame.get("i"), "value": frame.get("value"),
                                 "components": row})

            missing = [cid for cid, pts in per_component_points.items() if not pts]
            for cid in missing:
                warnings.append(f"component '{cid}' was never placed by the sweep")
            if not all_points:
                raise ValueError("sweep_envelope: no requested component was placed in any "
                                 "sweep frame; check the component ids")

            result = {
                "ok": True,
                "doc_id": d,
                "mate_id": out.get("mate_id"),
                "driver": out.get("driver"),
                "components": [c["id"] for c in comps],
                "envelope": _bbox_of(all_points),
                "per_component": {cid: _bbox_of(pts)
                                  for cid, pts in per_component_points.items() if pts},
                "per_step": per_step,
                "warnings": warnings,
            }
            if fuse:
                solids = []
                for c in comps:
                    cid = c["id"]
                    if not poses.get(cid):
                        continue
                    built = _build_swept_solid(c.get("name") or cid,
                                               _source_path(c), poses[cid])
                    built["component"] = cid
                    solids.append(built)
                    if not built.get("ok"):
                        result["ok"] = False
                result["swept_solids"] = solids
                if not result["ok"]:
                    result["error"] = ("one or more swept solids failed to build; the bbox "
                                       "envelope above is still valid")
            summary_box = result["envelope"]["size"]
            result["summary"] = (
                f"{len(trajectory)} steps | {len(result['per_component'])} components | "
                f"envelope {summary_box[0]:.4g} x {summary_box[1]:.4g} x "
                f"{summary_box[2]:.4g} mm")
            return _dumps(result)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------------- steering_turning_radius

    def _yaw_deg(m: list) -> float:
        """Heading of the body's local +X axis in the world XY plane (Z-up), in degrees."""
        return math.degrees(math.atan2(m[4], m[0]))

    @mcp.tool()
    def steering_turning_radius(steer_mate_id: Optional[str] = None,
                                start: Optional[float] = None,
                                stop: Optional[float] = None, steps: int = 5,
                                wheelbase_mm: Optional[float] = None,
                                track_mm: Optional[float] = None,
                                inner_wheel: Optional[str] = None,
                                doc_id: Optional[str] = None) -> str:
        """ACKERMANN TURNING RADIUS of a steered vehicle, from its own solved geometry.

        Sweeps the steer mate, reads each steered wheel's YAW (heading of its local +X axis
        about world +Z) at the two extremes of the sweep, and evaluates the closed-form
        Ackermann geometry — no new physics, no integration. Every number is reported with
        the measurement it came from, so an over/under-steer claim is checkable.

        wheelbase_mm / track_mm : supply them and they are used verbatim. Otherwise they are
                  DERIVED from the solved assembly under these assumptions (echoed in
                  `assumptions`, and the tool refuses rather than guessing when they do not
                  hold): world +Z is up; the components whose yaw changes across the sweep
                  are the steered axle; the other axle is the set of non-steered components
                  whose bbox size matches a steered wheel within 25% and whose height
                  matches within 25% of the wheel height; wheelbase = the longitudinal gap
                  between the two axle centres; track = the lateral spread of the steered
                  wheels (their bbox centres).
        inner_wheel : component id/name of the wheel on the INSIDE of the turn. Omitted, the
                  wheel with the larger |yaw| at each extreme is taken as the inner one
                  (true Ackermann: the inner wheel steers more).
        start/stop/steps : the steer mate sweep (`stop` required, `steps` >= 2). The pose at
                  `start` is taken as STRAIGHT AHEAD; steer angles are measured from it.

        Returns {ok, doc_id, mate_id, wheelbase_mm, track_mm, derived_from,
                 steered_components, extremes:[{value, inner, outer, inner_steer_deg,
                 outer_steer_deg, ackermann_outer_deg, ackermann_error_deg,
                 turn_center:[x,y], inner_wheel_radius_mm, outer_wheel_radius_mm,
                 centerline_radius_mm, outer_swept_radius_mm}], tightest, assumptions,
                 warnings}. A radius is null when that extreme is (near) straight ahead.
        """
        try:
            d = _doc(doc_id)
            steer_mate_id = _normalize_optional_slot(steer_mate_id)
            inner_wheel = _normalize_optional_slot(inner_wheel)
            steps = int(steps)
            if steps < 2:
                raise ValueError("steering_turning_radius: `steps` must be >= 2.")
            comps = _components(d)
            if not comps:
                raise ValueError("steering_turning_radius: document has no components.")

            out = _sweep(d, steer_mate_id, start, stop, steps)
            trajectory = out.get("trajectory") or []
            first, last = trajectory[0], trajectory[-1]
            xf0, xf1 = _frame_transforms(first), _frame_transforms(last)

            name_of = {c["id"]: (c.get("name") or c["id"]) for c in comps}
            steered = []
            for cid, m0 in xf0.items():
                m1 = xf1.get(cid)
                if m1 is None:
                    continue
                delta = _yaw_deg(m1) - _yaw_deg(m0)
                delta = (delta + 180.0) % 360.0 - 180.0
                if abs(delta) > _STEER_EPS_DEG:
                    steered.append(cid)
            if not steered:
                raise ValueError(
                    "steering_turning_radius: no component changes heading across the sweep "
                    f"— mate '{out.get('mate_id')}' does not steer anything.")

            # Local + world bbox of every placed component at the straight-ahead pose.
            locals_: dict = {}
            boxes: dict = {}
            for c in comps:
                cid = c["id"]
                if cid not in xf0:
                    continue
                try:
                    lb = _local_bbox(_source_path(c))
                except Exception:  # noqa: BLE001 — a component with no part file is not a wheel
                    continue
                locals_[cid] = lb
                boxes[cid] = _bbox_of([_xform_point(xf0[cid], p) for p in _corners(lb)])

            warnings = []
            assumptions = ["world +Z is up",
                           "the sweep's `start` pose is straight ahead",
                           "steer angle = yaw of the wheel's local +X about world +Z"]

            front = [boxes[cid] for cid in steered if cid in boxes]
            if not front:
                raise ValueError("steering_turning_radius: the steered components have no "
                                 "source geometry to locate an axle from.")
            front_center = [sum(b["center"][a] for b in front) / len(front)
                            for a in range(3)]

            # Longitudinal axis: from the other axle's centre toward the steered axle.
            derived_from = {"wheelbase": "argument", "track": "argument"}
            rear_center = None
            if wheelbase_mm is None or track_mm is None:
                wheel_size = sorted(front[0]["size"])
                wheel_h = front[0]["size"][2]
                rear_ids = []
                for cid, box in boxes.items():
                    if cid in steered:
                        continue
                    size = sorted(box["size"])
                    if any(ref <= 0 or abs(size[a] - ref) > _WHEEL_SIZE_TOL * ref
                           for a, ref in enumerate(wheel_size)):
                        continue
                    if wheel_h > 0 and abs(box["center"][2] - front_center[2]) > \
                            _WHEEL_Z_TOL * wheel_h:
                        continue
                    rear_ids.append(cid)
                if rear_ids:
                    rear_center = [sum(boxes[c]["center"][a] for c in rear_ids) /
                                   len(rear_ids) for a in range(3)]
                    assumptions.append(
                        "second axle recognised as components " +
                        ", ".join(sorted(name_of.get(c, c) for c in rear_ids)))
                elif wheelbase_mm is None:
                    raise ValueError(
                        "steering_turning_radius: could not recognise a second axle "
                        "(no non-steered component matches a steered wheel's size and "
                        "height). Pass wheelbase_mm (and track_mm) explicitly.")

            if rear_center is not None:
                axis = [front_center[0] - rear_center[0], front_center[1] - rear_center[1]]
                span = math.hypot(axis[0], axis[1])
                if span <= 1e-9:
                    raise ValueError("steering_turning_radius: the two axles are coincident "
                                     "in plan; pass wheelbase_mm explicitly.")
                u = [axis[0] / span, axis[1] / span]
            else:
                # Wheelbase given but no second axle: longitudinal axis = the world axis the
                # steered wheels are NOT spread along.
                u = [1.0, 0.0]
                assumptions.append("longitudinal axis assumed world +X (no second axle "
                                   "found to derive it from)")
                span = float(wheelbase_mm)
            v = [-u[1], u[0]]  # lateral, +Z x u

            def _lat(p) -> float:
                return p[0] * v[0] + p[1] * v[1]

            if wheelbase_mm is None:
                wheelbase_mm = span
                derived_from["wheelbase"] = "axle centres of the solved assembly"
            wheelbase_mm = float(wheelbase_mm)
            if not math.isfinite(wheelbase_mm) or wheelbase_mm <= 0:
                raise ValueError("steering_turning_radius: wheelbase_mm must be > 0.")

            if track_mm is None:
                lats = [_lat(b["center"]) for b in front]
                if len(lats) < 2 or (max(lats) - min(lats)) <= 1e-9:
                    raise ValueError(
                        "steering_turning_radius: only one steered wheel — track cannot be "
                        "derived. Pass track_mm explicitly.")
                track_mm = max(lats) - min(lats)
                derived_from["track"] = "lateral spread of the steered wheels"
            track_mm = float(track_mm)
            if not math.isfinite(track_mm) or track_mm <= 0:
                raise ValueError("steering_turning_radius: track_mm must be > 0.")

            centerline_lat = sum(_lat(b["center"]) for b in front) / len(front)
            rear_plan = (rear_center[:2] if rear_center is not None else
                         [front_center[0] - u[0] * wheelbase_mm,
                          front_center[1] - u[1] * wheelbase_mm])

            if inner_wheel is not None:
                pick = next((c for c in comps
                             if c.get("id") == inner_wheel or
                             (c.get("name") or "") == inner_wheel), None)
                if pick is None or pick["id"] not in steered:
                    raise ValueError(
                        f"steering_turning_radius: inner_wheel '{inner_wheel}' is not one of "
                        "the steered components (" +
                        ", ".join(name_of.get(c, c) for c in steered) + ").")
                inner_wheel = pick["id"]

            extremes = []
            for frame in (first, last):
                xf = _frame_transforms(frame)
                yaws = {cid: (_yaw_deg(xf[cid]) - _yaw_deg(xf0[cid]))
                        for cid in steered if cid in xf}
                yaws = {cid: (y + 180.0) % 360.0 - 180.0 for cid, y in yaws.items()}
                if inner_wheel is not None and inner_wheel in yaws:
                    inner_id = inner_wheel
                else:
                    inner_id = max(yaws, key=lambda c: abs(yaws[c]))
                outer_id = next((c for c in yaws if c != inner_id), None)
                di = abs(yaws[inner_id])
                do = abs(yaws[outer_id]) if outer_id else None
                row = {"value": frame.get("value"),
                       "inner": name_of.get(inner_id, inner_id),
                       "outer": name_of.get(outer_id, outer_id) if outer_id else None,
                       "inner_steer_deg": di,
                       "outer_steer_deg": do}
                if di <= _STEER_EPS_DEG:
                    row.update({"straight_ahead": True, "turn_center": None,
                                "inner_wheel_radius_mm": None,
                                "outer_wheel_radius_mm": None,
                                "centerline_radius_mm": None,
                                "outer_swept_radius_mm": None})
                    extremes.append(row)
                    continue
                di_r = math.radians(di)
                # Turn centre: on the non-steered axle line, offset laterally toward the
                # inner wheel by wheelbase/tan(inner steer) + half track from the centreline.
                inner_side = 1.0 if _lat(boxes[inner_id]["center"]) >= centerline_lat else -1.0
                r_center = wheelbase_mm / math.tan(di_r) + track_mm / 2.0
                center = [rear_plan[0] + v[0] * inner_side * r_center,
                          rear_plan[1] + v[1] * inner_side * r_center]
                r_inner = wheelbase_mm / math.sin(di_r)
                lat_outer = wheelbase_mm / math.tan(di_r) + track_mm
                r_outer = math.hypot(wheelbase_mm, lat_outer)
                ack_outer = math.degrees(math.atan2(wheelbase_mm, lat_outer))

                swept = 0.0
                for cid, m in xf.items():
                    lb = locals_.get(cid)
                    if lb is None:
                        continue
                    for p in _corners(lb):
                        w = _xform_point(m, p)
                        swept = max(swept, math.hypot(w[0] - center[0], w[1] - center[1]))
                row.update({
                    "straight_ahead": False,
                    "turn_center": center,
                    "inner_wheel_radius_mm": r_inner,
                    "outer_wheel_radius_mm": r_outer,
                    "centerline_radius_mm": r_center,
                    "ackermann_outer_deg": ack_outer,
                    "ackermann_error_deg": (do - ack_outer) if do is not None else None,
                    "outer_swept_radius_mm": swept})
                extremes.append(row)

            turning = [r for r in extremes if not r["straight_ahead"]]
            if not turning:
                warnings.append("neither extreme steers; the reported sweep is straight "
                                "ahead at both ends")
            tightest = (min(turning, key=lambda r: r["centerline_radius_mm"])
                        if turning else None)
            result = {
                "ok": True,
                "doc_id": d,
                "mate_id": out.get("mate_id"),
                "driver": out.get("driver"),
                "wheelbase_mm": wheelbase_mm,
                "track_mm": track_mm,
                "derived_from": derived_from,
                "steered_components": [name_of.get(c, c) for c in steered],
                "extremes": extremes,
                "tightest": tightest,
                "assumptions": assumptions,
                "warnings": warnings,
            }
            result["summary"] = (
                f"wheelbase {wheelbase_mm:.4g} mm, track {track_mm:.4g} mm | tightest "
                f"centreline radius {tightest['centerline_radius_mm']:.4g} mm at "
                f"{tightest['inner_steer_deg']:.4g} deg inner steer, outer swept "
                f"{tightest['outer_swept_radius_mm']:.4g} mm"
                if tightest else "no steering at either sweep extreme")
            return _dumps(result)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # -------------------------------------------------- motion_fea_handoff_sweep

    def _mate_value(doc_id: str, mate_id) -> float:
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        mates = [m for m in (tr.get("mates") or []) if isinstance(m, dict)]
        mate = (next((m for m in mates if m.get("id") == mate_id), None)
                if mate_id else (mates[0] if len(mates) == 1 else None))
        if mate is None:
            raise ValueError("motion_fea_handoff_sweep: `start` is required when the driven "
                             "mate's current value cannot be read.")
        value = mate.get("value")
        if isinstance(value, dict):
            value = value.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or \
                not math.isfinite(float(value)):
            raise ValueError("motion_fea_handoff_sweep: the driven mate has no numeric "
                             "value; pass `start` explicitly.")
        return float(value)

    @mcp.tool()
    def motion_fea_handoff_sweep(component: str, fixtures: list,
                                 mate_id: Optional[str] = None,
                                 positions: Optional[list] = None,
                                 start: Optional[float] = None,
                                 stop: Optional[float] = None, steps: int = 5,
                                 dynamics: Optional[dict] = None,
                                 material: Union[str, dict] = "steel",
                                 mesh_fineness: float = 0.5,
                                 doc_id: Optional[str] = None,
                                 background: bool = False,
                                 fixture_mate_id: Optional[str] = None,
                                 body_load_face: Optional[Union[str, dict]] = None) -> str:
        """Motion -> FEA at SEVERAL mechanism positions: which pose actually governs.

        A single `motion_fea_handoff` proves one snapshot. A mechanism's worst stress is not
        always at its worst joint reaction (the load direction rotates with the linkage), so
        this batches the SAME handoff — identical isolation, balance and refusal rules — at
        each requested position of the driven mate and reports the governing one.

        positions : driver values (deg or mm, the mate's unit) to solve at. Omitted, the
                  `steps` sweep samples across [start, stop] are used, so each position is
                  an exact swept sample. An explicit position that is not a sample snaps to
                  the nearest one (both values are reported).
        steps   : sweep samples (>= 3, inverse dynamics needs acceleration). This is also the
                  number of FEA solves when `positions` is omitted — the default of 5 is
                  deliberately small.
        background : return a job_id immediately and batch in a worker thread; poll
                  get_job(job_id). Otherwise the batch runs inline and hands off to the same
                  job table if it overruns the synchronous budget.

        Every other argument is passed straight through to `motion_fea_handoff`
        (`fixtures`, `fixture_mate_id`, `body_load_face`, `dynamics`, `material`,
        `mesh_fineness`).

        Returns {ok, doc_id, component, mate_id, positions:[{position,
                 position_requested, t_s, ok, max_von_mises_mpa, max_disp_mm, min_fos,
                 reactions, error?}], worst:{...}, n_failed, warnings}. `worst` is the
        position with the highest max_von_mises_mpa among the solved ones.
        """
        try:
            d = _doc(doc_id)
            mate_id = _normalize_optional_slot(mate_id)
            fixtures = require_list(fixtures, "fixtures",
                                    'a one-item list naming the grounded mate face')
            steps = int(steps)
            if steps < 3:
                raise ValueError("motion_fea_handoff_sweep: `steps` must be >= 3 "
                                 "(inverse dynamics needs acceleration).")
            if stop is None:
                raise ValueError("motion_fea_handoff_sweep: `stop` is required.")
            stop = float(stop)
            if start is None:
                start = _mate_value(d, mate_id)
            start = float(start)
            if not (math.isfinite(start) and math.isfinite(stop)):
                raise ValueError("motion_fea_handoff_sweep: start and stop must be finite.")
            if abs(stop - start) <= 1e-12:
                raise ValueError("motion_fea_handoff_sweep: start and stop are equal — "
                                 "there is no sweep to batch over.")
            dyn = coerce_json_or_repr({} if dynamics is None else dynamics)
            if not isinstance(dyn, dict):
                raise ValueError("motion_fea_handoff_sweep: dynamics must be an object.")
            duration_s = float(dyn.get("duration_s", 1.0) or 1.0)
            if not math.isfinite(duration_s) or duration_s <= 0.0:
                raise ValueError("motion_fea_handoff_sweep: dynamics.duration_s must be > 0.")

            samples = [start + (stop - start) * i / (steps - 1) for i in range(steps)]
            if positions is None:
                wanted = list(samples)
            else:
                wanted = [float(p) for p in require_list(
                    positions, "positions", "a list of driver values (deg or mm)")]
                if not wanted:
                    raise ValueError("motion_fea_handoff_sweep: `positions` is empty.")
                lo, hi = min(start, stop), max(start, stop)
                outside = [p for p in wanted if p < lo - 1e-9 or p > hi + 1e-9]
                if outside:
                    raise ValueError(
                        f"motion_fea_handoff_sweep: positions {outside} lie outside the "
                        f"sweep [{lo:g}, {hi:g}].")

            def _run() -> dict:
                rows = []
                warnings: list = []
                for want in wanted:
                    # `motion_fea_handoff` picks its frame by TIME; the sweep's time map is
                    # linear in the driver value, so a position is an exact time.
                    idx = min(range(steps), key=lambda i: abs(samples[i] - want))
                    t_s = duration_s * idx / (steps - 1)
                    row = {"position_requested": want, "position": samples[idx],
                           "t_s": t_s}
                    try:
                        out = _call_tool(
                            "motion_fea_handoff", component=component, fixtures=fixtures,
                            mate_id=mate_id, start=start, stop=stop, steps=steps, at=t_s,
                            dynamics=dyn, material=material, mesh_fineness=mesh_fineness,
                            doc_id=d, fixture_mate_id=fixture_mate_id,
                            body_load_face=body_load_face)
                    except Exception as exc:  # noqa: BLE001 — one bad pose must not lose the batch
                        row.update({"ok": False, "error": str(exc)})
                        rows.append(row)
                        continue
                    fea = out.get("fea") or {}
                    summary = fea.get("summary") or {}
                    balance = out.get("balance_diagnostics") or {}
                    row.update({
                        "ok": bool(out.get("ok")) and bool(fea.get("ok")),
                        "max_von_mises_mpa": summary.get("max_von_mises_mpa"),
                        "max_disp_mm": summary.get("max_disp_mm"),
                        "min_fos": summary.get("min_fos"),
                        "reactions": {
                            "applied_force_local_n":
                                balance.get("applied_nonfixture_force_sum_local_n"),
                            "applied_moment_local_nm":
                                balance.get(
                                    "applied_nonfixture_moment_sum_about_origin_local_nm"),
                            "fixture_reaction_force_local_n":
                                balance.get(
                                    "expected_fixture_boundary_reaction_force_local_n"),
                            "fixture_reaction_moment_local_nm":
                                balance.get(
                                    "expected_fixture_boundary_reaction_moment_"
                                    "about_origin_local_nm")},
                        "frame": out.get("frame"),
                        "simhandoff_id": (out.get("simhandoff") or {}).get("id"),
                    })
                    if not row["ok"]:
                        row["error"] = out.get("error") or fea.get("error")
                    for w in out.get("warnings") or []:
                        if w not in warnings:
                            warnings.append(w)
                    rows.append(row)

                solved = [r for r in rows
                          if r.get("ok") and isinstance(r.get("max_von_mises_mpa"),
                                                        (int, float))]
                worst = max(solved, key=lambda r: r["max_von_mises_mpa"]) if solved else None
                n_failed = len(rows) - len(solved)
                result = {
                    "ok": bool(solved) and n_failed == 0,
                    "doc_id": d,
                    "component": component,
                    "mate_id": mate_id,
                    "driver": {"start": start, "stop": stop, "steps": steps,
                               "duration_s": duration_s},
                    "positions": rows,
                    "worst": worst,
                    "n_failed": n_failed,
                    "warnings": warnings,
                }
                if worst is not None:
                    result["summary"] = (
                        f"{len(solved)}/{len(rows)} positions solved | worst at driver "
                        f"{worst['position']:.4g}: {worst['max_von_mises_mpa']:.4g} MPa, "
                        f"min FoS {worst['min_fos']}")
                else:
                    result["summary"] = f"no position solved ({len(rows)} attempted)"
                    result["error"] = "every requested position failed its handoff"
                return result

            if background:
                jid = _start_job("motion_fea_sweep", _run)
                return _dumps({"ok": True, "job_id": jid, "status": "running",
                               "doc_id": d, "component": component,
                               "n_positions": len(wanted),
                               "note": f"batching {len(wanted)} motion->FEA handoffs in the "
                                       f"background; poll get_job('{jid}')."})
            return _dumps(_solve_sync_or_handoff("motion_fea_sweep", _run))
        except Exception as e:  # noqa: BLE001
            return _err(e)
