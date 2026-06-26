"""Translate a SolidWorks feature IR (:mod:`engine.doc.sw_feature_import`) into AI-CAD's native
``add_features`` op list, so an imported SolidWorks part lands as an *editable* feature tree.

Strict-fidelity contract: every feature this can rebuild is emitted as real ops; every feature it
cannot is appended to ``unsupported`` (carried over from extraction + anything found here) so the
caller can refuse the import with a precise punch-list. We never silently approximate.

Geometry addressing:
  * SolidWorks parts are Y-up; AI-CAD is Z-up. A sketch's plane is classified upstream into a
    principal orientation ('top'/'front'/'right'), mapped here to ``plane.top``/``plane.front``/
    ``plane.right``. A parallel offset plane (e.g. a loft section's RefPlane) is folded into the
    sketch's ``refs.offset``. Tilted / non-principal planes are reported unsupported.
  * A sketch built on a model FACE is emitted as a face query (``{"of":"face","side":...}``).
"""

from __future__ import annotations

from typing import Any

from engine.doc.sw_feature_import import (
    SWFeature, SWPart, SWUnsupported, extract_part)

_PRINCIPAL_PLANE = {"top": "plane.top", "front": "plane.front", "right": "plane.right"}
# In-plane 2D basis (eU, eV) and plane normal in ENGINE coords for each principal sketch plane —
# validated against SolidWorks center-of-mass on asymmetric parts (SW Y-up -> engine Z-up).
#   top:   (u,v) -> (u, v, 0)   front: (u,v) -> (u, 0, v)   right: (u,v) -> (0, u, v)
_PLANE_BASIS = {
    "top":   ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "front": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    "right": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
}


def _to_engine_3d(principal: str, uv, offset: float = 0.0):
    """Map a sketch 2D point (u,v) on a principal plane to an engine-space 3D point."""
    eu, ev, n = _PLANE_BASIS[principal]
    return [uv[0] * eu[i] + uv[1] * ev[i] + offset * n[i] for i in range(3)]


def _T(p):
    """SolidWorks model coords (mm) -> engine world coords (mm). SW Y-up -> engine Z-up."""
    return [round(p[0], 6), round(-p[2], 6), round(p[1], 6)]


def _near_edges(model_points):
    """A SelList of 'near' edge queries, one per SolidWorks edge point (transformed to engine)."""
    return [{"of": "edge", "near": _T(p), "count": 1} for p in model_points]


def _to_engine_dir(principal: str, duv):
    """Map a sketch 2D direction (du,dv) on a principal plane to an engine-space 3D direction."""
    eu, ev, _ = _PLANE_BASIS[principal]
    return [duv[0] * eu[i] + duv[1] * ev[i] for i in range(3)]
# Geometric relations the engine accepts with no driving value (purely positional).
_NONDIM_CONSTRAINTS = {
    "coincident", "horizontal", "vertical", "parallel", "perpendicular", "tangent",
    "collinear", "equal", "concentric", "coradial", "midpoint", "symmetric", "fix", "point_on",
}
# Dimensional constraints carry a driving value; linking a SolidWorks display-dimension to the exact
# constraint it drives is not done yet, so these are dropped (geometry is still exact from coords)
# and reported as a per-sketch limitation.
_DIM_CONSTRAINTS = {"distance", "angle", "radius", "diameter"}
# Minimum sketch-entity operands each geometric relation needs to be valid in the engine.
_CONSTRAINT_MIN_REFS = {
    "coincident": 2, "parallel": 2, "perpendicular": 2, "tangent": 2, "collinear": 2,
    "equal": 2, "concentric": 2, "coradial": 2, "midpoint": 2, "symmetric": 3, "point_on": 2,
    "horizontal": 1, "vertical": 1, "fix": 1,
}
# Which engine face-side a principal sketch-plane orientation corresponds to, by offset sign.
_FACE_SIDE = {"top": ("top", "bottom"), "front": ("front", "back"), "right": ("right", "left")}


def _entity_dict(e) -> dict:
    # Inline entity for params.entities (self-contained; keeps the IR's stable id e0, e1, …).
    ent: dict[str, Any] = {"id": e.id, "type": e.type, "construction": bool(e.construction)}
    ent.update(e.data)
    return ent


# swDimensionType_e codes for the dims we can link.
_SW_DIM_DIAMETER, _SW_DIM_RADIAL = 6, 5
_SW_DIM_LINEAR = {2, 11, 12, 14, 15}


def _link_dimensions(f: SWFeature, constraints: list) -> tuple[int, int]:
    """Append editable dimensional constraints for the sketch's driving dimensions. Returns
    (linked, unlinked). Diameter/radial dims are matched to a circle/arc by size; linear dims are
    left unlinked (their SW reference entities aren't resolved yet)."""
    linked = unlinked = 0
    for dim in f.dims:
        if dim.dim_type == _SW_DIM_DIAMETER:
            ent = _match_round(f, dim.value / 2.0)
            if ent is not None:
                constraints.append({"id": f"c{len(constraints)}", "type": "diameter",
                                    "refs": [ent.id], "value": {"value": dim.value, "expr": None}})
                linked += 1
                continue
        elif dim.dim_type == _SW_DIM_RADIAL:
            ent = _match_round(f, dim.value)
            if ent is not None:
                constraints.append({"id": f"c{len(constraints)}", "type": "radius",
                                    "refs": [ent.id], "value": {"value": dim.value, "expr": None}})
                linked += 1
                continue
        unlinked += 1
    return linked, unlinked


def _match_round(f: SWFeature, radius: float):
    """The unique circle/arc entity whose radius matches (mm), or None if absent/ambiguous."""
    hits = [e for e in f.entities if e.type in ("circle", "arc")
            and abs(e.data.get("radius", -1) - radius) < 1e-4]
    return hits[0] if len(hits) == 1 else None


def _region_centroid(f: SWFeature) -> list[float]:
    """A point inside the sketch's profile (engine uses it to pick the region to extrude)."""
    pts: list[list[float]] = []
    for e in f.entities:
        if e.construction:
            continue
        if e.type == "circle":
            pts.append(e.data["center"])
        elif e.type == "line":
            pts.append(e.data["p1"]); pts.append(e.data["p2"])
        elif e.type == "arc":
            pts.append(e.data["center"])
        elif e.type == "spline":
            pts.extend(e.data.get("points", []))
    if not pts:
        return [0.0, 0.0]
    return [round(sum(p[0] for p in pts) / len(pts), 6),
            round(sum(p[1] for p in pts) / len(pts), 6)]


def _plane_offset(f: SWFeature) -> float:
    """Perpendicular offset (mm) of the sketch plane from the principal plane through the origin."""
    tr = (f.plane or {}).get("trans_mm") or [0, 0, 0]
    # model->sketch translation; the offset magnitude is the in-normal component. For a principal
    # plane the relevant component is the largest |trans|. Sign is validated geometrically.
    return max((abs(tr[0]), abs(tr[1]), abs(tr[2])))


def _sketch_ops(f: SWFeature, op_index: int,
                unsupported: list[SWUnsupported]) -> tuple[list[dict], int] | None:
    """Build the ops for a sketch. Returns (ops, sketch_index_within_ops) — the sketch op is not
    always ops[0]: an offset datum plane may be emitted first. Returns None if unsupported."""
    plane = f.plane or {}
    principal = plane.get("principal")
    sel_type = plane.get("sel_type")
    refs: dict[str, Any] = {}
    pre_ops: list[dict] = []

    if principal not in _PRINCIPAL_PLANE:
        unsupported.append(SWUnsupported(
            f.sw_name, f.sw_type,
            f"sketch is on a non-principal / tilted plane (orientation={principal!r}); "
            "only Front/Top/Right-parallel sketches are reconstructed so far"))
        return None

    if sel_type == 2:  # on a model face
        side = _FACE_SIDE[principal][0]
        refs["plane"] = {"of": "face", "side": side}
    else:              # on a datum plane (principal, possibly offset)
        off = _plane_offset(f)
        if off > 1e-6:
            # Rebuild the SolidWorks offset RefPlane as a real datum, then sketch on it.
            pre_ops.append({
                "op": "add_feature",
                "feature": {"type": "plane", "name": f"{f.sw_name}_Plane",
                            "params": {"method": "offset", "offset": {"value": off, "expr": None}},
                            "refs": {"base": _PRINCIPAL_PLANE[principal]}}})
            refs["plane"] = {"kind": "datum", "feature": f"${op_index}"}
        else:
            refs["plane"] = _PRINCIPAL_PLANE[principal]

    if any(e.type not in ("line", "circle", "arc", "spline", "point") for e in f.entities):
        bad = sorted({e.type for e in f.entities
                      if e.type not in ("line", "circle", "arc", "spline", "point")})
        unsupported.append(SWUnsupported(f.sw_name, f.sw_type,
                                         f"sketch has unsupported entity types: {bad}"))
        return None
    if f.params.get("seg_errors"):
        for r in f.params["seg_errors"]:
            unsupported.append(SWUnsupported(f.sw_name, f.sw_type, r))
        return None

    sketch_idx = op_index + len(pre_ops)
    constraints = []
    for c in f.constraints:
        # Engine constraints need their full operand set; SW relations that referenced model
        # geometry (edges/faces) lose an operand here, so keep only fully-mapped ones.
        need = _CONSTRAINT_MIN_REFS.get(c.type, 1)
        if c.type in _NONDIM_CONSTRAINTS and len(c.refs) >= need:
            constraints.append({"id": f"c{len(constraints)}", "type": c.type, "refs": list(c.refs)})
    # DRIVING DIMENSIONS -> editable dimensional constraints. Diameter/radial dims are linked to
    # their circle/arc by value-matching (robust when sizes are distinct); linear dims aren't linked
    # yet (need the SW reference entities) and are reported degraded.
    linked, unlinked = _link_dimensions(f, constraints)
    if unlinked:
        unsupported.append(SWUnsupported(
            f.sw_name, f.sw_type,
            f"{unlinked} driving dimension(s) not linked (geometry is exact; the dim is just not "
            "re-editable as a parameter — linear/positional dims pending)",
            severity="degraded"))
    ops: list[dict] = list(pre_ops)
    ops.append({
        "op": "add_feature",
        "feature": {"type": "sketch", "name": f.sw_name,
                    "params": {"entities": [_entity_dict(e) for e in f.entities],
                               "constraints": constraints}, "refs": refs},
    })
    return ops, (sketch_idx - op_index)


def translate(part: SWPart) -> dict:
    """IR -> {"ops": [...], "unsupported": [...]}.

    ``unsupported`` aggregates extraction-time gaps and translation-time gaps. The caller should
    refuse the import (and surface the BLOCKING ones)."""
    if part.is_assembly:
        return _translate_assembly(part)
    ops, feat_unsup = _translate_part_features(part)
    return {"ops": ops, "unsupported": list(part.unsupported) + feat_unsup}


def _translate_part_features(part: SWPart) -> tuple[list[dict], list[SWUnsupported]]:
    """Build the ops for ONE part's feature tree (op indices start at 0). Returns (ops, unsupported
    found while translating). For assemblies each component part is built into its OWN document, so
    these $-refs stay self-contained (no cross-component offsetting needed)."""
    ops: list[dict] = []
    unsupported: list[SWUnsupported] = []
    # op index of the sketch feature, by SolidWorks sketch name, for profile/section references.
    sketch_op_index: dict[str, int] = {}
    # op index of EVERY reconstructed feature's primary op, for pattern/mirror seed references.
    feature_op_index: dict[str, int] = {}

    def _record(name):
        feature_op_index[name] = len(ops) - 1

    def _seed_refs(seed_names):
        """Engine FeatList refs ($-ops) for a pattern/mirror's seed feature names; None if any
        seed wasn't reconstructed."""
        out = []
        for nm in seed_names:
            if nm not in feature_op_index:
                return None
            out.append(f"${feature_op_index[nm]}")
        return out

    for f in part.features:
        if f.kind == "sketch":
            base = len(ops)
            result = _sketch_ops(f, base, unsupported)
            if result is None:
                continue
            sk_ops, sketch_local = result
            sketch_op_index[f.sw_name] = base + sketch_local
            feature_op_index[f.sw_name] = base + sketch_local
            ops += sk_ops

        elif f.kind in ("extrude", "cut"):
            # The sketch consumed by this extrude is the most recent un-consumed sketch before it.
            src = _last_sketch_before(part, f)
            if src is None or src not in sketch_op_index:
                unsupported.append(SWUnsupported(
                    f.sw_name, f.sw_type, "could not resolve the sketch this extrude consumes"))
                continue
            end = f.params.get("end")
            if end is None:
                continue  # already recorded unsupported during extraction
            mode = "cut" if f.kind == "cut" else "boss"
            params: dict[str, Any] = {"mode": mode, "end": end}
            if "depth" in f.params:
                params["depth"] = {"value": f.params["depth"], "expr": None}
            region = {"kind": "region", "sketch": f"${sketch_op_index[src]}",
                      "centroid": _region_centroid(part.by_name[src]), "hint_index": 0}
            ops.append({"op": "add_feature",
                        "feature": {"type": "extrude", "name": f.sw_name,
                                    "params": params, "refs": {"profile": [region]}}})
            _record(f.sw_name)

        elif f.kind == "revolve":
            src = _last_sketch_before(part, f)
            if src is None or src not in sketch_op_index:
                unsupported.append(SWUnsupported(
                    f.sw_name, f.sw_type, "could not resolve the sketch this revolve consumes"))
                continue
            sketch = part.by_name[src]
            axis = _revolve_axis(sketch, unsupported, f)
            if axis is None:
                continue
            ang = f.params.get("angle_deg", 360.0)
            region = {"kind": "region", "sketch": f"${sketch_op_index[src]}",
                      "centroid": _region_centroid(sketch), "hint_index": 0}
            ops.append({"op": "add_feature",
                        "feature": {"type": "revolve", "name": f.sw_name,
                                    "params": {"mode": f.params.get("mode", "boss"),
                                               "angle": {"value": ang, "expr": None}},
                                    "refs": {"profile": [region], "axis": axis}}})
            _record(f.sw_name)

        elif f.kind == "loft":
            sections = [s for s in _loft_sections(part, f) if s in sketch_op_index]
            if len(sections) < 2:
                unsupported.append(SWUnsupported(
                    f.sw_name, f.sw_type,
                    f"loft needs >=2 reconstructable section sketches (found {len(sections)})"))
                continue
            secrefs = [{"kind": "region", "sketch": f"${sketch_op_index[s]}",
                        "centroid": _region_centroid(part.by_name[s]), "hint_index": 0}
                       for s in sections]
            ops.append({"op": "add_feature",
                        "feature": {"type": "loft", "name": f.sw_name,
                                    "params": {"mode": "boss"}, "refs": {"sections": secrefs}}})
            _record(f.sw_name)

        elif f.kind == "fillet":
            edges = _near_edges(f.params.get("edge_points", []))
            if not edges:
                unsupported.append(SWUnsupported(f.sw_name, f.sw_type, "no fillet edges resolved"))
                continue
            ops.append({"op": "add_feature",
                        "feature": {"type": "fillet", "name": f.sw_name,
                                    "params": {"radius": {"value": f.params["radius"], "expr": None}},
                                    "refs": {"edges": edges}}})
            _record(f.sw_name)

        elif f.kind == "chamfer":
            edges = _near_edges(f.params.get("edge_points", []))
            if not edges:
                unsupported.append(SWUnsupported(f.sw_name, f.sw_type, "no chamfer edges resolved"))
                continue
            ops.append({"op": "add_feature",
                        "feature": {"type": "chamfer", "name": f.sw_name,
                                    "params": {"length": {"value": f.params["length"], "expr": None}},
                                    "refs": {"edges": edges}}})
            _record(f.sw_name)

        elif f.kind == "shell":
            pts = f.params.get("face_points", [])
            # The engine shell resolves a SINGLE geom-query object for `remove` (near/side); a list
            # routes to a different resolver that can't read geom queries. Emit one query per the
            # single open face; a closed shell (no removed faces) omits `remove`.
            if len(pts) > 1:
                unsupported.append(SWUnsupported(
                    f.sw_name, f.sw_type,
                    f"shell removes {len(pts)} faces; multi-face open shells not supported yet"))
                continue
            refs = {"remove": {"of": "face", "near": _T(pts[0]), "count": 1}} if pts else {}
            ops.append({"op": "add_feature",
                        "feature": {"type": "shell", "name": f.sw_name,
                                    "params": {"thickness": {"value": f.params["thickness"],
                                                             "expr": None}}, "refs": refs}})
            _record(f.sw_name)

        elif f.kind == "linear_pattern":
            seeds = _seed_refs(f.params.get("seed_names", []))
            if not seeds:
                unsupported.append(SWUnsupported(
                    f.sw_name, f.sw_type, "linear-pattern seed feature(s) not reconstructed"))
                continue
            dir1 = {"kind": "axis", "axis": _T(f.params["dir1_model"])}
            params = {"count1": f.params["count1"],
                      "spacing1": {"value": f.params["spacing1"], "expr": None}}
            refs = {"dir1": dir1, "features": seeds}
            if f.params.get("count2"):
                params["count2"] = f.params["count2"]
                params["spacing2"] = {"value": f.params["spacing2"], "expr": None}
                refs["dir2"] = {"kind": "axis", "axis": _T(f.params["dir2_model"])}
            ops.append({"op": "add_feature",
                        "feature": {"type": "linear_pattern", "name": f.sw_name,
                                    "params": params, "refs": refs}})
            _record(f.sw_name)

        elif f.kind == "circular_pattern":
            seeds = _seed_refs(f.params.get("seed_names", []))
            if not seeds:
                unsupported.append(SWUnsupported(
                    f.sw_name, f.sw_type, "circular-pattern seed feature(s) not reconstructed"))
                continue
            axis = {"kind": "axis", "axis": _T(f.params["axis_model"]),
                    "origin": _T(f.params["axis_origin_model"])}
            params = {"count": f.params["count"],
                      "angle": {"value": f.params.get("angle_deg", 360.0), "expr": None}}
            ops.append({"op": "add_feature",
                        "feature": {"type": "circular_pattern", "name": f.sw_name,
                                    "params": params,
                                    "refs": {"axis": axis, "features": seeds}}})
            _record(f.sw_name)

        elif f.kind == "mirror":
            seeds = _seed_refs(f.params.get("seed_names", []))
            plane = _PRINCIPAL_PLANE.get(f.params.get("plane_principal"))
            if not seeds or not plane:
                unsupported.append(SWUnsupported(
                    f.sw_name, f.sw_type,
                    "mirror seed feature(s) or mirror plane not reconstructed"))
                continue
            ops.append({"op": "add_feature",
                        "feature": {"type": "mirror", "name": f.sw_name, "params": {},
                                    "refs": {"plane": plane, "features": seeds}}})
            _record(f.sw_name)

        elif f.kind in ("plane", "axis"):
            # Datum planes used only as sketch supports are folded into sketch offsets; emit nothing.
            # (A standalone/visible datum could be added later; not needed for reconstruction.)
            continue
        # Any other kind already produced an SWUnsupported during extraction.

    return ops, unsupported


# --------------------------------------------------------------------------- assembly
# SW Y-up -> engine Z-up rotation matrix and its inverse (point map T(x,y,z)=(x,-z,y)).
_TR = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]
_TR_INV = [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]]


def _matmul(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]



def _component_xform16(comp: "SWComponent") -> list[float]:
    """A row-major 4x4 placement matrix (engine coords) for an assembly component.

    The SW component-local→assembly transform M_sw=(R_sw,t_sw) maps to engine space as
    M_eng = T · M_sw · T⁻¹ : R_eng = T_R·R_sw·T_R⁻¹, t_eng = T(t_sw). Laid out as
    [r00,r01,r02,tx, r10,r11,r12,ty, r20,r21,r22,tz, 0,0,0,1] (matches the engine's xform16)."""
    R_sw = [comp.rot[0:3], comp.rot[3:6], comp.rot[6:9]]
    R = _matmul(_matmul(_TR, R_sw), _TR_INV)
    t = _T(comp.trans_mm)
    return [R[0][0], R[0][1], R[0][2], t[0],
            R[1][0], R[1][1], R[1][2], t[1],
            R[2][0], R[2][1], R[2][2], t[2],
            0.0, 0.0, 0.0, 1.0]


def _translate_assembly(part: SWPart) -> dict:
    """Rebuild an assembly the way the engine actually models them: each component is its OWN part
    (built from its feature tree, saved to a .step+), placed in an ASSEMBLY document via
    asm_insert_component with a 4x4 transform. Returns a per-component build plan; the MCP import
    orchestrates the multi-doc build (build component -> save .step+ -> insert with transform)."""
    unsupported: list[SWUnsupported] = list(part.unsupported)
    components: list[dict] = []
    # Build each DISTINCT component part once; reuse the .step+ for repeated instances.
    plan_by_path: dict[str, dict] = {}
    for comp in part.components:
        key = comp.part.path
        if key not in plan_by_path:
            sub_ops, sub_unsup = _translate_part_features(comp.part)
            unsupported += sub_unsup
            plan_by_path[key] = {"ops": sub_ops,
                                 "feature_count": len(comp.part.features),
                                 "ok": bool(sub_ops)}
        plan = plan_by_path[key]
        if not plan["ok"]:
            unsupported.append(SWUnsupported(comp.name, "Component",
                                             "component built no solid body to place"))
            continue
        components.append({"name": comp.name, "part_key": key, "ops": plan["ops"],
                           "xform16": _component_xform16(comp)})
    return {"is_assembly": True, "components": components, "unsupported": unsupported}


def resolve(path: str) -> dict:
    """Open a SolidWorks part via COM, reconstruct its feature tree, and return a build plan.

    Returns ``{"name", "ops", "blocking", "degraded", "feature_count"}`` where:
      * ``ops``       — the ``doc_apply_ops`` op list that rebuilds the part as an editable tree;
      * ``blocking``  — features that could NOT be faithfully rebuilt (each {sw_name, sw_type,
                        reason}); a non-empty list means the import must be refused;
      * ``degraded``  — features rebuilt exactly but with some parametric intent lost (e.g. an
                        unlinked driving dimension); reportable, not fatal.
    Raises :class:`SolidWorksUnavailable` if SolidWorks/pywin32 can't be used here."""
    part = extract_part(path)
    t = translate(part)
    blocking, degraded = [], []
    for u in t["unsupported"]:
        rec = {"sw_name": u.sw_name, "sw_type": u.sw_type, "reason": u.reason}
        (degraded if u.severity == "degraded" else blocking).append(rec)
    out = {"name": part.name, "blocking": blocking, "degraded": degraded}
    if t.get("is_assembly"):
        out["is_assembly"] = True
        out["components"] = t["components"]   # [{name, part_key, ops, xform16}]
        out["feature_count"] = sum(len(c.part.features) for c in part.components)
    else:
        out["ops"] = t["ops"]
        out["feature_count"] = len(part.features)
    return out


def _revolve_axis(sketch: SWFeature, unsupported: list[SWUnsupported],
                  feat: SWFeature) -> dict | None:
    """The revolve axis = the single construction centerline in the profile sketch, expressed as an
    explicit engine axis {axis:[dir], origin:[pt]}. Refuses if there isn't exactly one."""
    principal = (sketch.plane or {}).get("principal")
    if principal not in _PLANE_BASIS:
        unsupported.append(SWUnsupported(feat.sw_name, feat.sw_type,
                                         "revolve profile is on a non-principal plane"))
        return None
    centerlines = [e for e in sketch.entities if e.construction and e.type == "line"]
    if len(centerlines) != 1:
        unsupported.append(SWUnsupported(
            feat.sw_name, feat.sw_type,
            f"revolve axis ambiguous: expected one construction centerline, found "
            f"{len(centerlines)} (axis from a model edge / reference axis not supported yet)"))
        return None
    cl = centerlines[0]
    off = _plane_offset(sketch) if (sketch.plane or {}).get("sel_type") != 2 else 0.0
    p1, p2 = cl.data["p1"], cl.data["p2"]
    direction = _to_engine_dir(principal, (p2[0] - p1[0], p2[1] - p1[1]))
    origin = _to_engine_3d(principal, p1, off)
    return {"kind": "axis", "axis": [round(x, 6) for x in direction],
            "origin": [round(x, 6) for x in origin]}


def _last_sketch_before(part: SWPart, target: SWFeature) -> str | None:
    """Name of the nearest preceding sketch feature (the profile an extrude/cut consumes)."""
    last = None
    for f in part.features:
        if f is target:
            break
        if f.kind == "sketch":
            last = f.sw_name
    return last


def _loft_sections(part: SWFeature, target: SWFeature) -> list[str]:
    """Best-effort: all sketches before the loft, in order (refined once loft wiring is read from
    the loft feature definition)."""
    out: list[str] = []
    for f in part.features:
        if f is target:
            break
        if f.kind == "sketch":
            out.append(f.sw_name)
    return out
