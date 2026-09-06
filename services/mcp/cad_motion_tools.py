"""CAD kinematic-evidence MCP tools: sweep_envelope, steering_turning_radius,
motion_fea_handoff_sweep.

Every tool here is a COMPOSITION over the primitives that already exist —
``simulate_motion`` and ``motion_fea_handoff`` (services/mcp/cad_asm_tools.py) — resolved
out of the shared FastMCP registry at call time, so their validation, dialect handling and
dispatch guard run exactly once and are never re-implemented here. What this module adds is
the mechanism-level arithmetic a rover/linkage claim needs: a swept ENVELOPE (and optionally
the swept SOLID), a KINEMATIC/Ackermann TURNING RADIUS, and a batched motion→FEA sweep
across several mechanism positions.

Two ways to MOVE the mechanism, and both end in the same trajectory shape:

* a DIMENSIONAL mate (``mate_id``) driven through ``simulate_motion`` — the classic path;
* a ``steer`` spec — a rigid rotation of named components about a measured axis, applied to
  a DETACHED clone of the document with the mate network re-solved around it. A real
  steering assembly poses its uprights with concentric/coincident mates about kingpin axes
  and owns no angle/distance mate at all, so ``simulate_motion`` has nothing to drive;
  the ``steer`` spec drives the geometry the assembly actually has.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import json
import math
import os

from .cad_asm_tools import _solve_report
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

#: A driven upright's wheel is recognised as a carried DISC: two of its three bbox
#: extents agree to this fraction, and it rides with the upright to within this many
#: degrees of yaw.
_WHEEL_ROUND_TOL = 0.15
_CARRIED_YAW_TOL_DEG = 0.5

#: Turning radii are quoted in feet as well as metres on a vehicle claim.
_MM_PER_FT = 304.8

_IDENT16 = (1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0)

#: Every key a `steer` / `rear_steer` spec may carry (aliases included). An unknown key is
#: refused rather than silently ignored — a mis-spelled `angle` used to steer nothing.
_STEER_SPEC_KEYS = frozenset({
    "component", "components", "comp", "comps",
    "axis", "axes", "kingpin_axis", "kingpin_axes",
    "mate_id", "mate", "mate_ids", "mates",
    "angle_deg", "angles_deg", "angle", "angles", "steer_deg",
    "wheel", "wheels",
})


def _mat_mul16(a: list, b: list) -> list:
    """Row-major 4x4 product a@b."""
    out = [0.0] * 16
    for i in range(4):
        r = i * 4
        for j in range(4):
            out[r + j] = (a[r] * b[j] + a[r + 1] * b[4 + j] +
                          a[r + 2] * b[8 + j] + a[r + 3] * b[12 + j])
    return out


def _rigid_inverse16(m: list) -> list:
    """Inverse of a rigid row-major 4x4 (R^T, -R^T t) — exact, no general solve."""
    r = [[m[0], m[1], m[2]], [m[4], m[5], m[6]], [m[8], m[9], m[10]]]
    t = [m[3], m[7], m[11]]
    ti = [-sum(r[k][i] * t[k] for k in range(3)) for i in range(3)]
    return [r[0][0], r[1][0], r[2][0], ti[0],
            r[0][1], r[1][1], r[2][1], ti[1],
            r[0][2], r[1][2], r[2][2], ti[2],
            0.0, 0.0, 0.0, 1.0]


def _axis_rotation16(point, direction, angle_deg: float) -> list:
    """Row-major 4x4 rotating `angle_deg` (right-hand rule) about the LINE (point, dir).

    This is the kingpin drive: translate the axis point to the origin, rotate about the
    axis direction (Rodrigues), translate back. Exact for any axis in space, so a real
    caster/kingpin-inclination axis is honoured rather than flattened to world +Z.
    """
    n = [float(v) for v in direction]
    norm = math.sqrt(sum(v * v for v in n))
    if norm <= 1e-12:
        raise ValueError("steer axis `dir` is a zero-length vector")
    n = [v / norm for v in n]
    th = math.radians(float(angle_deg))
    c, s, k = math.cos(th), math.sin(th), 1.0 - math.cos(th)
    x, y, z = n
    r = [[c + k * x * x, k * x * y - s * z, k * x * z + s * y],
         [k * y * x + s * z, c + k * y * y, k * y * z - s * x],
         [k * z * x - s * y, k * z * y + s * x, c + k * z * z]]
    p = [float(v) for v in point]
    t = [p[i] - sum(r[i][j] * p[j] for j in range(3)) for i in range(3)]
    return [r[0][0], r[0][1], r[0][2], t[0],
            r[1][0], r[1][1], r[1][2], t[1],
            r[2][0], r[2][1], r[2][2], t[2],
            0.0, 0.0, 0.0, 1.0]


def _turn_center_2d(lines: list) -> Optional[dict]:
    """Instantaneous centre of rotation of a set of plan-view WHEEL AXLE lines.

    A wheel that rolls without slipping turns about a point on its own axle line, so the
    vehicle's turn centre is where every wheel's axle line meets. `lines` are
    ((px, py), (dx, dy)) pairs with UNIT directions; the centre returned minimises the
    squared perpendicular distance to each line (the exact intersection whenever the
    geometry is consistent — see the module tests).

    Returns {center:[x,y], residual_mm} where `residual_mm` is the worst perpendicular
    miss — 0 for a true Ackermann pose, (track/2)*sin(steer) for parallel steer, so an
    inconsistent linkage is reported rather than averaged away. None when the axle lines
    are parallel: that is straight ahead, and the turn centre is at infinity.
    """
    a00 = a01 = a11 = b0 = b1 = 0.0
    normals = []
    for (p, d) in lines:
        n0, n1 = -float(d[1]), float(d[0])
        mag = math.hypot(n0, n1)
        if mag <= 1e-12:
            continue
        n0, n1 = n0 / mag, n1 / mag
        c = n0 * float(p[0]) + n1 * float(p[1])
        normals.append((n0, n1, c))
        a00 += n0 * n0
        a01 += n0 * n1
        a11 += n1 * n1
        b0 += n0 * c
        b1 += n1 * c
    if len(normals) < 2:
        return None
    det = a00 * a11 - a01 * a01
    # Scale-free rank test: the normals are unit, so trace(A) == len(normals) and a
    # parallel (straight-ahead) bundle drives det to 0 independently of vehicle size.
    if det <= 1e-9 * max(1.0, (a00 + a11) ** 2):
        return None
    cx = (a11 * b0 - a01 * b1) / det
    cy = (a00 * b1 - a01 * b0) / det
    residual = max(abs(n0 * cx + n1 * cy - c) for (n0, n1, c) in normals)
    return {"center": [cx, cy], "residual_mm": residual}


def _provisional_longitudinal(front: list) -> list:
    """A longitudinal unit direction in plan, BEFORE the second axle is known.

    The wheels of one axle spread LATERALLY, so the vehicle's longitudinal axis is
    perpendicular to the widest spread among them. With fewer than two wheels there is
    nothing to measure and world +X is the stated fallback.
    """
    best = None
    for i in range(len(front)):
        for j in range(i + 1, len(front)):
            a, b = front[i]["center"], front[j]["center"]
            dx, dy = a[0] - b[0], a[1] - b[1]
            d = math.hypot(dx, dy)
            if d > 1e-9 and (best is None or d > best[0]):
                best = (d, -dy / d, dx / d)
    return [1.0, 0.0] if best is None else [best[1], best[2]]


def _clash_row(hit: dict) -> dict:
    """One `eval_interference` pair, without its overlap MESH.

    A steer sweep runs the check once per sample and reports every one; the triangle soup
    that proves an overlap would dwarf the whole reply, and the pair plus its volume and
    overlap box is what a clearance claim is made from.
    """
    keep = ("a", "b", "volume", "volume_mm3", "overlap_volume_mm3", "distance",
            "overlap_bbox", "kind")
    return {k: hit[k] for k in keep if k in hit}


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
            # A refusal (nothing to drive) is not a failed solve — pass it through as-is
            # so the model reads the teaching text, not "did not converge" over the top.
            raise ValueError(
                str(out.get("error") or out.get("summary") or "motion sweep failed"))
        if not (out.get("trajectory") or []):
            raise ValueError("motion sweep returned no trajectory")
        return out

    # ---------------------------------------------------- steer drive (no mate needed)

    def _clone_detached(doc_id: str, tool: str) -> str:
        """A DETACHED copy of `doc_id`, opened from the document's own file.

        The steer drive AUTHORS component poses (`asm_edit_component` + `fixed`), which is
        a real mutation — so it lands on a clone: the caller's document keeps its
        revision, dirty flag, undo history and mate ownership. `detach_path=True` leaves
        the clone unbound to the file, so nothing here can overwrite the source.
        """
        row = next((r for r in (cb.call("doc_list", {}) or {}).get("docs", [])
                    if isinstance(r, dict) and r.get("doc_id") == doc_id), {})
        path = row.get("path")
        if not path or not os.path.exists(str(path)):
            raise ValueError(
                f"{tool}(steer=...) drives a DETACHED CLONE of the assembly so your live "
                "document is never re-posed, and a clone can only be opened from a file. "
                f"Document {doc_id} has no saved file — call save_part(path=...) (or "
                "save_assembly) first, then steer it.")
        clone = (cb.call("doc_open", {"path": str(path), "detach_path": True})
                 or {}).get("doc_id")
        if not clone:
            raise ValueError(f"doc_open({path}) returned no doc_id; cannot steer a clone.")
        return clone

    def _comp_transforms(doc_id: str) -> dict:
        """{component_id: row-major 16} straight out of the document's SOLVED components."""
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        out: dict = {}
        for c in (tr.get("components") or []):
            if not isinstance(c, dict) or c.get("suppressed"):
                continue
            cid = str(c.get("id") or "")
            if not cid:
                continue
            tf = c.get("transform")
            out[cid] = (_finite16(tf, f"transform of '{cid}'")
                        if isinstance(tf, list) and len(tf) == 16 else list(_IDENT16))
        return out

    def _axis_from_mate(doc_id: str, mate_id: str, cid: str, xf: dict) -> dict:
        """The WORLD axis a mate resolves to, for the steered component `cid`.

        A concentric/coincident kingpin mate stores each ref as the geometry query that
        matched it, and a cylindrical/circular match carries its own `axis` + `centroid`
        in the component's LOCAL frame — the measured kingpin axis, already in the
        document. Prefer the ref that belongs to `cid`; otherwise take the other side's
        (a satisfied concentric mate puts both on the same world line) and say so.
        """
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        mates = [m for m in (tr.get("mates") or []) if isinstance(m, dict)]
        mate = next((m for m in mates if m.get("id") == mate_id), None)
        if mate is None:
            known = ", ".join(f"{m.get('id')}({m.get('type')})" for m in mates) or "none"
            raise ValueError(f"steer mate_id '{mate_id}' is not a mate of this assembly. "
                             f"Mates: {known}")
        refs = [r for r in (mate.get("refs") or []) if isinstance(r, dict)]
        axial = [r for r in refs
                 if isinstance((r.get("selector") or {}).get("axis"), list)
                 and isinstance((r.get("selector") or {}).get("centroid"), list)]
        if not axial:
            raise ValueError(
                f"steer mate_id '{mate_id}' ({mate.get('type')}) resolves no AXIS: neither "
                "reference is a cylindrical face or circular edge, so there is no kingpin "
                "line to rotate about. Pass axis={'point':[x,y,z],'dir':[x,y,z]} (e.g. from "
                "fit_cylinder_axis on the kingpin bore).")
        ref = next((r for r in axial if r.get("component") == cid), axial[0])
        owner = str(ref.get("component") or "")
        m = xf.get(owner)
        if m is None:
            raise ValueError(f"steer mate_id '{mate_id}': its reference component "
                             f"'{owner}' is not placed in this assembly.")
        sel = ref["selector"]
        pt = _xform_point(m, [float(v) for v in sel["centroid"]])
        d = [float(v) for v in sel["axis"]]
        dw = [m[0] * d[0] + m[1] * d[1] + m[2] * d[2],
              m[4] * d[0] + m[5] * d[1] + m[6] * d[2],
              m[8] * d[0] + m[9] * d[1] + m[10] * d[2]]
        return {"point": pt, "dir": dw,
                "source": f"mate '{mate_id}' ref on component '{owner}'"}

    def _axis_literal(raw, label: str) -> dict:
        """{'point':[3],'dir':[3]} out of an explicit axis spec (dir aliases accepted)."""
        v = coerce_json_or_repr(raw)
        if not isinstance(v, dict):
            raise ValueError(f"{label} must be an object "
                             "{'point':[x,y,z],'dir':[x,y,z]}, got "
                             f"{type(v).__name__}")
        pt = next((v[k] for k in ("point", "origin", "at", "axis_point", "location")
                   if isinstance(v.get(k), (list, tuple))), None)
        dr = next((v[k] for k in ("dir", "direction", "axis", "axis_dir", "normal")
                   if isinstance(v.get(k), (list, tuple))), None)
        if pt is None or dr is None or len(pt) != 3 or len(dr) != 3:
            raise ValueError(
                f"{label} needs a 3-number `point` ON the axis and a 3-number `dir` ALONG "
                f"it, e.g. {{'point':[520,-410,180],'dir':[0,0,1]}} — got keys "
                f"{sorted(v)}. fit_cylinder_axis on the kingpin bore returns exactly "
                "axis_point/axis_dir.")
        p = [float(x) for x in pt]
        d = [float(x) for x in dr]
        if not all(math.isfinite(x) for x in p + d):
            raise ValueError(f"{label} is non-finite")
        if math.sqrt(sum(x * x for x in d)) <= 1e-12:
            raise ValueError(f"{label} `dir` is a zero-length vector")
        return {"point": p, "dir": d, "source": "explicit axis"}

    def _steer_drives(doc_id: str, spec, label: str, xf: dict) -> list:
        """A `steer`/`rear_steer` spec -> [{component,name,angle_deg,axis,wheel}].

        Refuses instead of guessing: an unknown key, a components/angles length mismatch,
        or a missing axis is a teaching error, because every one of those silently steered
        nothing in earlier hand-rolled attempts.
        """
        v = coerce_json_or_repr(spec)
        if not isinstance(v, dict):
            raise ValueError(
                f"{label} must be an object, e.g. {label}={{'components':['FL','FR'], "
                "'axis':{'point':[x,y,z],'dir':[0,0,1]}, 'angle_deg':30}} — got "
                f"{type(v).__name__}.")
        unknown = sorted(set(v) - _STEER_SPEC_KEYS)
        if unknown:
            raise ValueError(
                f"{label}: unknown key(s) {unknown}. It takes components (or component), "
                "axis (or mate_id), angle_deg (or angles_deg) and optional wheel/wheels.")
        raw_comps = next((v[k] for k in ("components", "component", "comps", "comp")
                          if v.get(k) is not None), None)
        if raw_comps is None:
            raise ValueError(f"{label}: name the component(s) to steer, e.g. "
                             f"{label}={{'components':['upright_FL','upright_FR'], ...}}.")
        if isinstance(raw_comps, str):
            raw_comps = [raw_comps]
        comps = _resolve_components(doc_id, raw_comps)
        n = len(comps)

        raw_ang = next((v[k] for k in ("angles_deg", "angle_deg", "angles", "angle",
                                       "steer_deg") if v.get(k) is not None), None)
        if raw_ang is None:
            raise ValueError(f"{label}: give the steer angle — angle_deg=<deg> for every "
                             "named component, or angles_deg=[...] one per component.")
        raw_ang = coerce_json_or_repr(raw_ang)
        if isinstance(raw_ang, (int, float)) and not isinstance(raw_ang, bool):
            angles = [float(raw_ang)] * n
        else:
            angles = [float(a) for a in require_list(
                raw_ang, f"{label}.angles_deg", "one angle in degrees per component")]
            if len(angles) != n:
                raise ValueError(
                    f"{label}: {len(angles)} angle(s) for {n} component(s). Give one "
                    "angles_deg entry per component (a true Ackermann pair steers the "
                    "inner wheel more), or a single angle_deg for parallel steer.")
        for a in angles:
            if not math.isfinite(a) or abs(a) > 90.0:
                raise ValueError(f"{label}: steer angle {a} deg is not a finite angle in "
                                 "(-90, 90); a wheel cannot pass 90 deg of steer.")

        raw_mate = next((v[k] for k in ("mate_ids", "mate_id", "mates", "mate")
                         if v.get(k) is not None), None)
        raw_axis = next((v[k] for k in ("axes", "axis", "kingpin_axes", "kingpin_axis")
                         if v.get(k) is not None), None)
        if raw_mate is None and raw_axis is None:
            raise ValueError(
                f"{label}: give the kingpin axis to rotate about — axis={{'point':[x,y,z],"
                "'dir':[x,y,z]} (one axis, or a list/mapping one per component), or "
                "mate_id='<concentric kingpin mate>' to take the axis the mate already "
                "resolves.")
        axes: list = []
        for i, c in enumerate(comps):
            cid, cname = c["id"], (c.get("name") or c["id"])
            if raw_mate is not None:
                mids = raw_mate if isinstance(raw_mate, list) else [raw_mate] * n
                if len(mids) != n:
                    raise ValueError(f"{label}: {len(mids)} mate_ids for {n} components.")
                axes.append(_axis_from_mate(doc_id, str(mids[i]), cid, xf))
                continue
            a = coerce_json_or_repr(raw_axis)
            if isinstance(a, list):
                if len(a) != n:
                    raise ValueError(f"{label}: {len(a)} axes for {n} components — give "
                                     "one axis per component, or one shared axis.")
                axes.append(_axis_literal(a[i], f"{label}.axis[{i}]"))
            elif isinstance(a, dict) and not any(
                    k in a for k in ("point", "origin", "at", "axis_point", "location")):
                hit = a.get(cid, a.get(cname))
                if hit is None:
                    raise ValueError(
                        f"{label}.axis is keyed by component but has no entry for "
                        f"'{cname}' ({cid}); keys are {sorted(a)}.")
                axes.append(_axis_literal(hit, f"{label}.axis['{cname}']"))
            else:
                axes.append(_axis_literal(a, f"{label}.axis"))

        raw_wheel = next((v[k] for k in ("wheels", "wheel") if v.get(k) is not None), None)
        wheels: list = [None] * n
        if raw_wheel is not None:
            if isinstance(raw_wheel, str):
                raw_wheel = [raw_wheel] * n
            picks = _resolve_components(doc_id, require_list(
                raw_wheel, f"{label}.wheels", "one wheel component per steered component"))
            if len(picks) != n:
                raise ValueError(f"{label}: {len(picks)} wheel(s) for {n} component(s).")
            wheels = [p["id"] for p in picks]

        return [{"component": c["id"], "name": c.get("name") or c["id"],
                 "angle_deg": angles[i], "axis": axes[i], "wheel": wheels[i]}
                for i, c in enumerate(comps)]

    def _steer_sweep(doc_id: str, steer, rear_steer, steps: int,
                     check_interference: bool, tool: str) -> dict:
        """Drive named components about their kingpin axes; return a simulate_motion-shaped
        result so every consumer downstream is identical to the dimensional-mate path.

        `steps` samples scale the requested angles from 0 (straight ahead) to 1 (full
        lock), so a sweep exists even though the assembly owns no drivable mate. Each
        sample AUTHORS the rotated pose on the clone: `asm_edit_component` carrying both
        `transform` and `fixed:true`, because an unfixed mated component keeps its warm
        seed and its authored transform is an engine-level no-op (contracts/doc_ops.md §8)
        — the drive would report ok and move nothing. Grounding the driven upright is also
        the physically right statement: the steering input owns that pose, and the mate
        network re-solves the tie rods / hubs / wheels around it.
        """
        clone = _clone_detached(doc_id, tool)
        try:
            base = _comp_transforms(clone)
            names = {}
            for c in _components(clone):
                names[str(c.get("id"))] = c.get("name") or str(c.get("id"))
            drives = _steer_drives(clone, steer, "steer", base)
            for dr in drives:
                dr["axle"] = "front"
            if rear_steer is not None:
                rear = _steer_drives(clone, rear_steer, "rear_steer", base)
                for dr in rear:
                    dr["axle"] = "rear"
                drives += rear
            seen: set = set()
            for d in drives:
                if d["component"] in seen:
                    raise ValueError(
                        f"component '{d['name']}' is driven twice (steer and rear_steer "
                        "cannot both own the same component's pose).")
                seen.add(d["component"])
            missing = [d["name"] for d in drives if d["component"] not in base]
            if missing:
                raise ValueError(f"steered component(s) {missing} are not placed in this "
                                 "assembly.")

            primary = max(drives, key=lambda d: abs(d["angle_deg"]))["angle_deg"]
            trajectory = []
            first_hit = None
            interferences = []
            for i in range(steps):
                scale = i / (steps - 1) if steps > 1 else 1.0
                ops = []
                for d in drives:
                    r = _axis_rotation16(d["axis"]["point"], d["axis"]["dir"],
                                         scale * d["angle_deg"])
                    ops.append({"op": "asm_edit_component", "id": d["component"],
                                "transform": _mat_mul16(r, base[d["component"]]),
                                "fixed": True})
                cb.call("doc_apply_ops", {"doc_id": clone, "with_tess": False, "ops": ops})
                sr = cb.call("doc_solve_mates", {"doc_id": clone}) or {}
                rb = sr.get("rebuild") or {}
                rep = _solve_report(rb)
                xf = _comp_transforms(clone)
                frame = {"i": i, "value": scale * primary, "scale": scale,
                         "dof": rb.get("dof"), "converged": rep["converged"],
                         "max_residual": rep["worst_residual"],
                         "bodies": [{"component": cid, "name": names.get(cid, cid),
                                     "transform": m} for cid, m in xf.items()]}
                if not rep["converged"]:
                    frame["solve_error"] = rep["message"]
                if check_interference:
                    ir = cb.call("eval_interference",
                                 {"doc_id": clone, "include_coincident": False,
                                  "min_clearance_mm": 0.0}) or {}
                    hits = [_clash_row(h) for h in (ir.get("interferences") or [])
                            if isinstance(h, dict)]
                    frame["collisions"] = hits
                    if hits and first_hit is None:
                        first_hit = {"step": i, "value": frame["value"],
                                     "detail": hits[:8]}
                        interferences = hits[:8]
                trajectory.append(frame)

            bad = [f["i"] for f in trajectory if not f["converged"]]
            out = {
                "ok": not bad,
                "doc_id": doc_id,
                "mate_id": None,
                "driver": {"type": "steer", "unit": "deg", "start": 0.0, "stop": primary,
                           "steps": steps, "samples": len(trajectory),
                           "components": [d["name"] for d in drives],
                           "angles_deg": [d["angle_deg"] for d in drives],
                           "axes": [{"point": d["axis"]["point"], "dir": d["axis"]["dir"],
                                     "source": d["axis"]["source"]} for d in drives]},
                "trajectory": trajectory,
                "steer_drives": drives,
                "interference": {"checked": bool(check_interference),
                                 "first_collision_step":
                                     first_hit["step"] if first_hit else None,
                                 "first_collision_value":
                                     first_hit["value"] if first_hit else None,
                                 "detail": interferences},
            }
            if bad:
                out["error"] = (
                    "the mate network did not converge around the steered pose at step(s) "
                    f"{bad}: " + str(next(f.get("solve_error") for f in trajectory
                                          if not f["converged"])))
            return out
        finally:
            try:
                cb.call("doc_close", {"doc_id": clone})
            except Exception:  # noqa: BLE001 — a clone we cannot close is not a result
                pass

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
                       steer: Optional[dict] = None, rear_steer: Optional[dict] = None,
                       doc_id: Optional[str] = None) -> str:
        """SWEPT ENVELOPE of a moving mechanism — the volume its parts need to be given.

        Drives the mechanism and, at every sample, transforms each listed component's own
        bounding box (all 8 corners, so a rotated pose is not under-reported) into world
        coordinates. Returns the union envelope over the whole sweep plus the per-step,
        per-component boxes — the numbers a clearance claim ("the wheel never leaves its
        arch") is actually made from.

        TWO WAYS TO DRIVE IT:
          * `mate_id` + [start, stop] — one dimensional (angle/distance) mate, swept with
            `simulate_motion`.
          * `steer` — a STEER SWEEP that needs no dimensional mate: named components are
            rigidly rotated about a kingpin axis on a detached clone and the mate network
            is re-solved around them. Use this for a steering assembly whose uprights are
            posed by concentric/coincident mates (there is no angle mate to drive). Same
            spec as `steering_turning_radius`:
              steer={"components":["upright_FL","upright_FR"],
                     "axis":{"point":[x,y,z],"dir":[x,y,z]},   # or "mate_id":"m7"
                     "angle_deg":30}                            # or "angles_deg":[30,26.4]
            `rear_steer` takes the same shape for a 4WS rear axle. `steps` samples scale
            the angles from 0 (straight ahead) to full lock, so the envelope covers the
            whole steering range, not just the extreme.

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
        AXIS-ALIGNED in world coordinates. On a steer sweep `driver.type` is "steer" and
        `driver.axes` echoes the kingpin axis each component was rotated about.
        """
        try:
            d = _doc(doc_id)
            mate_id = _normalize_optional_slot(mate_id)
            steer = _normalize_optional_slot(steer)
            rear_steer = _normalize_optional_slot(rear_steer)
            comps = _resolve_components(d, _normalize_optional_slot(components))
            steps = int(steps)
            if steps < 2:
                raise ValueError("sweep_envelope: `steps` must be >= 2.")
            if fuse and steps > 64:
                raise ValueError("sweep_envelope: fuse=True with more than 64 steps is one "
                                 "boolean per step — reduce `steps`.")
            if steer is None and rear_steer is not None:
                raise ValueError("sweep_envelope: `rear_steer` is the 4WS companion to "
                                 "`steer` — pass `steer` too (or drive the rear axle with "
                                 "`steer` alone).")
            local = {c["id"]: _local_bbox(_source_path(c)) for c in comps}

            if steer is not None:
                if mate_id is not None or start is not None or stop is not None:
                    raise ValueError(
                        "sweep_envelope: `steer` and the mate sweep (mate_id/start/stop) "
                        "are two different drives — pass one or the other.")
                out = _steer_sweep(d, steer, rear_steer, steps, False, "sweep_envelope")
                if not out.get("ok"):
                    raise ValueError("steer sweep did not converge: " +
                                     str(out.get("error") or "unknown"))
            else:
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

    def _disc_like(size) -> bool:
        """A wheel/tyre bbox: two extents nearly equal (the disc) and one smaller (width)."""
        s = sorted(float(x) for x in size)
        return (s[2] > 0.0 and abs(s[2] - s[1]) <= _WHEEL_ROUND_TOL * s[2]
                and s[0] < s[1] * (1.0 - _WHEEL_ROUND_TOL))

    @mcp.tool()
    def steering_turning_radius(steer_mate_id: Optional[str] = None,
                                start: Optional[float] = None,
                                stop: Optional[float] = None, steps: int = 5,
                                wheelbase_mm: Optional[float] = None,
                                track_mm: Optional[float] = None,
                                inner_wheel: Optional[str] = None,
                                steer: Optional[dict] = None,
                                rear_steer: Optional[dict] = None,
                                check_interference: bool = False,
                                doc_id: Optional[str] = None) -> str:
        """TURNING RADIUS of a steered vehicle, from its own solved geometry.

        Poses the steering, reads every wheel's YAW (heading of its local +X about world
        +Z) and its AXLE LINE from the solved transforms, and intersects those axle lines:
        a wheel that rolls without slipping turns about a point on its own axle line, so
        the vehicle's turn centre is where the lines meet and the turning radius is that
        centre's distance from the vehicle's longitudinal centreline. No new physics, no
        integration. The closed-form Ackermann numbers are reported beside it, so an
        over/under-steer claim is checkable against the geometry that produced it.

        TWO WAYS TO POSE THE STEERING:

        1. `steer` — DRIVE THE UPRIGHTS DIRECTLY. No dimensional mate required: the named
           components are rigidly rotated about their kingpin axis on a DETACHED clone of
           the document and the mate network is re-solved around them (the driven
           component is grounded for that solve — the steering input owns its pose). This
           is the path for a real steering assembly, whose uprights are positioned by
           concentric/coincident mates about measured kingpin axes and own no angle mate
           at all.
               steer={"components": ["upright_FL", "upright_FR"],
                      "axis": {"point": [x,y,z], "dir": [x,y,z]},
                      "angle_deg": 30}
             components  : one or more component ids/names to rotate (also `component`).
             axis        : the kingpin line — {"point":[x,y,z],"dir":[x,y,z]} in world mm
                           (fit_cylinder_axis returns exactly axis_point/axis_dir). Pass
                           ONE axis, a LIST of axes (one per component, for the real
                           left/right kingpins), or a MAPPING keyed by component.
             mate_id     : instead of `axis` — take the axis the named concentric/
                           coincident kingpin mate already resolves (its cylindrical/
                           circular reference carries the measured axis).
             angle_deg   : one angle applied to every named component (parallel steer), or
             angles_deg  : one angle per component — a true Ackermann pair steers the
                           inner wheel more, and the pair's consistency comes back as
                           `axle_line_residual_mm`.
             wheel/wheels: optional — the component whose centre defines a corner's axle
                           line, when the driven upright does not itself carry the wheel.
                           Omitted, a rigidly-carried disc-shaped component is used (and
                           named in `assumptions`), else the driven component's own centre.
           `rear_steer` takes the same spec for a 4WS rear axle; the turn centre then
           moves off the rear axle (reported as `turn_center_station_mm`) and the radius
           halves for an equal-and-opposite rear angle.
           `steps` samples scale the requested angles from 0 (straight ahead) to full
           lock, so `check_interference` can name the FIRST angle that clashes.

        2. `steer_mate_id` + start/stop/steps — sweep one dimensional (angle/distance)
           mate with `simulate_motion`. The pose at `start` is STRAIGHT AHEAD and steer
           angles are measured from it (`stop` required, `steps` >= 2).

        wheelbase_mm / track_mm : supply them and they are used verbatim for the
                  closed-form Ackermann cross-check. Otherwise they are DERIVED from the
                  solved assembly under these assumptions (echoed in `assumptions`, and
                  the tool refuses rather than guessing when they do not hold): world +Z
                  is up; the steered axle is the components named in `steer` (or, on the
                  mate path, those whose yaw changes across the sweep); the other axle is
                  `rear_steer`'s components, or the non-steered components whose bbox size
                  matches a steered wheel within 25% and whose height matches within 25%
                  of the wheel height; wheelbase = the longitudinal gap between the two
                  axle centres; track = the lateral spread of the steered wheels.
        inner_wheel : component id/name of the wheel on the INSIDE of the turn. Omitted,
                  the wheel with the larger |yaw| at each extreme is taken as the inner
                  one (true Ackermann: the inner wheel steers more).
        check_interference : evaluate solid overlap at every sampled angle and report the
                  FIRST one that clashes (`first_interference_deg`) — the steering-lock
                  limit the geometry actually imposes.

        Returns {ok, doc_id, mate_id, driver, wheelbase_mm, track_mm, derived_from,
                 steered_components, turning_radius_mm/_m/_ft (the tightest sampled),
                 first_interference_deg, interference, extremes:[{value, inner, outer,
                 inner_steer_deg, outer_steer_deg, ackermann_outer_deg,
                 ackermann_error_deg, turn_center:[x,y], inner_wheel_radius_mm,
                 outer_wheel_radius_mm, centerline_radius_mm, outer_swept_radius_mm,
                 turning_radius_mm/_m/_ft, kinematic:{turn_center, turn_center_station_mm,
                 turning_radius_mm/_m/_ft, axle_line_residual_mm, outer_swept_radius_mm,
                 inner, outer, wheels:[{component, name, axle, steer_angle_deg,
                 requested_deg, radius_mm, radius_ft}]}}], tightest, assumptions,
                 warnings}. A radius is null when that extreme is (near) straight ahead;
        `kinematic` is null when the axle lines stay parallel (no turn) or no wheel axle
        could be read. `axle_line_residual_mm` is the worst perpendicular miss between the
        turn centre and a wheel's axle line: 0 for a consistent Ackermann pose,
        (track/2)*sin(steer) for parallel steer.
        """
        try:
            d = _doc(doc_id)
            steer_mate_id = _normalize_optional_slot(steer_mate_id)
            inner_wheel = _normalize_optional_slot(inner_wheel)
            steer = _normalize_optional_slot(steer)
            rear_steer = _normalize_optional_slot(rear_steer)
            steps = int(steps)
            if steps < 2:
                raise ValueError("steering_turning_radius: `steps` must be >= 2.")
            if steer is None and rear_steer is not None:
                raise ValueError(
                    "steering_turning_radius: `rear_steer` is the 4WS companion to "
                    "`steer` — pass `steer` too (or drive the rear axle with `steer`).")
            comps = _components(d)
            if not comps:
                raise ValueError("steering_turning_radius: document has no components.")
            name_of = {c["id"]: (c.get("name") or c["id"]) for c in comps}

            if steer is not None:
                if steer_mate_id is not None or start is not None or stop is not None:
                    raise ValueError(
                        "steering_turning_radius: `steer` drives the uprights directly and "
                        "`steer_mate_id`/start/stop sweeps a dimensional mate — pass one "
                        "or the other, not both.")
                out = _steer_sweep(d, steer, rear_steer, steps, check_interference,
                                   "steering_turning_radius")
                if not out.get("ok"):
                    raise ValueError("steer sweep did not converge: " +
                                     str(out.get("error") or "unknown"))
            else:
                out = _sweep(d, steer_mate_id, start, stop, steps,
                             check_interference=check_interference)
            trajectory = out.get("trajectory") or []
            first, last = trajectory[0], trajectory[-1]
            xf0, xf1 = _frame_transforms(first), _frame_transforms(last)

            yaw_delta: dict = {}
            for cid, m0 in xf0.items():
                m1 = xf1.get(cid)
                if m1 is None:
                    continue
                delta = _yaw_deg(m1) - _yaw_deg(m0)
                yaw_delta[cid] = (delta + 180.0) % 360.0 - 180.0
            moved = [cid for cid, y in yaw_delta.items() if abs(y) > _STEER_EPS_DEG]

            drives = out.get("steer_drives") or []
            wheel_arg = {dr["component"]: dr["wheel"] for dr in drives if dr.get("wheel")}
            requested = {dr["component"]: dr["angle_deg"] for dr in drives}
            if drives:
                steered = [dr["component"] for dr in drives if dr["axle"] == "front"]
                rear_driven = [dr["component"] for dr in drives if dr["axle"] == "rear"]
            else:
                steered = list(moved)
                rear_driven = []
            if not steered:
                raise ValueError(
                    "steering_turning_radius: no component changes heading across the "
                    f"sweep — mate '{out.get('mate_id')}' does not steer anything. If the "
                    "uprights are posed by concentric/coincident kingpin mates there is no "
                    "dimensional mate to drive: pass steer={'components':[...], "
                    "'axis':{'point':[...],'dir':[...]}, 'angle_deg':...} instead.")

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
                           "the straight-ahead pose is the sweep's first sample",
                           "steer angle = yaw of the wheel's local +X about world +Z",
                           "a wheel's axle line at the straight-ahead pose is the vehicle "
                           "lateral direction, carried rigidly with the wheel"]

            front = [boxes[cid] for cid in steered if cid in boxes]
            if not front:
                raise ValueError("steering_turning_radius: the steered components have no "
                                 "source geometry to locate an axle from.")
            front_center = [sum(b["center"][a] for b in front) / len(front)
                            for a in range(3)]

            # The second axle: named by `rear_steer` (the caller's word, taken as given),
            # else recognised by wheel similarity.
            derived_from = {"wheelbase": "argument" if wheelbase_mm is not None else None,
                            "track": "argument" if track_mm is not None else None}
            rear_ids = [cid for cid in rear_driven if cid in boxes]
            if rear_ids:
                assumptions.append("second axle driven by `rear_steer`: " +
                                   ", ".join(sorted(name_of.get(c, c) for c in rear_ids)))
            else:
                wheel_size = sorted(front[0]["size"])
                wheel_h = front[0]["size"][2]
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
                # A set of look-alike wheels is only an AXLE if they share a longitudinal
                # station. Without this, one steered wheel among four identical ones made
                # the other three "the rear axle" and put its centre in the middle of the
                # vehicle — a wheelbase and a longitudinal direction that are both fiction.
                if len(rear_ids) > 1:
                    prov = _provisional_longitudinal(front)
                    lons = [boxes[c]["center"][0] * prov[0] + boxes[c]["center"][1] * prov[1]
                            for c in rear_ids]
                    if max(lons) - min(lons) > 0.5 * max(front[0]["size"]):
                        warnings.append(
                            "the wheel-like non-steered components (" +
                            ", ".join(sorted(name_of.get(c, c) for c in rear_ids)) +
                            ") do not share one longitudinal station, so they are not a "
                            "single axle; the second axle was NOT derived from them. Pass "
                            "wheelbase_mm/track_mm, or name the rear axle with rear_steer.")
                        rear_ids = []
                if rear_ids:
                    assumptions.append(
                        "second axle recognised as components " +
                        ", ".join(sorted(name_of.get(c, c) for c in rear_ids)))
                elif wheelbase_mm is None:
                    raise ValueError(
                        "steering_turning_radius: could not recognise a second axle "
                        "(no non-steered component matches a steered wheel's size and "
                        "height, or they do not form one axle). Pass wheelbase_mm (and "
                        "track_mm) explicitly.")
            rear_center = ([sum(boxes[c]["center"][a] for c in rear_ids) / len(rear_ids)
                            for a in range(3)] if rear_ids else None)

            if rear_center is not None:
                axis = [front_center[0] - rear_center[0], front_center[1] - rear_center[1]]
                span = math.hypot(axis[0], axis[1])
                if span <= 1e-9:
                    raise ValueError("steering_turning_radius: the two axles are coincident "
                                     "in plan; pass wheelbase_mm explicitly.")
                u = [axis[0] / span, axis[1] / span]
            else:
                # Wheelbase given but no second axle: longitudinal axis = world +X.
                u = [1.0, 0.0]
                assumptions.append("longitudinal axis assumed world +X (no second axle "
                                   "found to derive it from)")
                span = float(wheelbase_mm)
            v = [-u[1], u[0]]  # lateral, +Z x u

            def _lat(p) -> float:
                return p[0] * v[0] + p[1] * v[1]

            def _lon(p) -> float:
                return p[0] * u[0] + p[1] * u[1]

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
            vehicle_lat = ((_lat(front_center) + _lat(rear_center)) / 2.0
                           if rear_center is not None else _lat(front_center))

            # Which component's centre defines each corner's axle line. A driven UPRIGHT is
            # not the wheel: its bbox centre sits at the kingpin, not on the axle. Explicit
            # `wheel` wins; else the nearest rigidly-carried disc; else the driven body.
            carried = [cid for cid in moved
                       if cid in boxes and cid not in steered and cid not in rear_ids]
            wheel_of: dict = {}
            claimed: set = set()
            for cid in steered + rear_ids:
                if cid in wheel_arg:
                    wheel_of[cid] = wheel_arg[cid]
                    continue
                if _disc_like(boxes[cid]["size"]):
                    wheel_of[cid] = cid
                    continue
                cands = sorted(
                    (x for x in carried if x not in claimed and _disc_like(boxes[x]["size"])
                     and abs(yaw_delta.get(x, 0.0) - yaw_delta.get(cid, 0.0))
                     <= _CARRIED_YAW_TOL_DEG),
                    key=lambda x: sum((boxes[x]["center"][a] - boxes[cid]["center"][a]) ** 2
                                      for a in range(3)))
                if cands:
                    wheel_of[cid] = cands[0]
                    claimed.add(cands[0])
                    assumptions.append(
                        f"axle line of '{name_of.get(cid, cid)}' taken from the wheel it "
                        f"carries, '{name_of.get(cands[0], cands[0])}'")
                else:
                    wheel_of[cid] = cid
                    warnings.append(
                        f"'{name_of.get(cid, cid)}' is not disc-shaped and carries no "
                        "recognisable wheel; its own bbox centre was used as the axle "
                        "point (pass steer={... 'wheel': '<wheel component>'} to be exact)")
            axle_of = {cid: "front" for cid in steered}
            axle_of.update({cid: "rear" for cid in rear_ids})

            def _kinematic(xf: dict) -> Optional[dict]:
                """Turn centre + per-wheel radii from the POSED wheel axle lines."""
                lines, rows = [], []
                for cid in steered + rear_ids:
                    w = wheel_of[cid]
                    m1, m0 = xf.get(w), xf0.get(w)
                    if m1 is None or m0 is None:
                        continue
                    delta = _mat_mul16(m1, _rigid_inverse16(m0))
                    pt = _xform_point(delta, boxes[w]["center"])
                    ax = [delta[0] * v[0] + delta[1] * v[1],
                          delta[4] * v[0] + delta[5] * v[1]]
                    mag = math.hypot(ax[0], ax[1])
                    if mag <= 1e-9:
                        # `_kinematic` runs once per extreme; one wheel must not warn twice.
                        note = (f"'{name_of.get(w, w)}' axle is vertical in this pose; it "
                                "was left out of the turn-centre fit")
                        if note not in warnings:
                            warnings.append(note)
                        continue
                    ax = [ax[0] / mag, ax[1] / mag]
                    lines.append(((pt[0], pt[1]), (ax[0], ax[1])))
                    rows.append({"component": w, "name": name_of.get(w, w),
                                 "axle": axle_of[cid],
                                 "steer_angle_deg": (_yaw_deg(m1) - _yaw_deg(m0) + 180.0)
                                 % 360.0 - 180.0,
                                 "requested_deg": requested.get(cid),
                                 "point": [pt[0], pt[1]]})
                tc = _turn_center_2d(lines)
                if tc is None or len(rows) < 2:
                    return None
                cx, cy = tc["center"]
                for r in rows:
                    r["radius_mm"] = math.hypot(r["point"][0] - cx, r["point"][1] - cy)
                    r["radius_ft"] = r["radius_mm"] / _MM_PER_FT
                    r.pop("point")
                fronts = [r for r in rows if r["axle"] == "front"]
                radius = abs((cx * v[0] + cy * v[1]) - vehicle_lat)
                swept = 0.0
                for cid, m in xf.items():
                    lb = locals_.get(cid)
                    if lb is None:
                        continue
                    for p in _corners(lb):
                        w = _xform_point(m, p)
                        swept = max(swept, math.hypot(w[0] - cx, w[1] - cy))
                return {"turn_center": [cx, cy],
                        "turn_center_station_mm": _lon([cx, cy]) - _lon(rear_plan),
                        "turning_radius_mm": radius,
                        "turning_radius_m": radius / 1000.0,
                        "turning_radius_ft": radius / _MM_PER_FT,
                        "axle_line_residual_mm": tc["residual_mm"],
                        "outer_swept_radius_mm": swept,
                        "inner": min(fronts, key=lambda r: r["radius_mm"]) if fronts
                                 else None,
                        "outer": max(fronts, key=lambda r: r["radius_mm"]) if fronts
                                 else None,
                        "wheels": rows}

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
                kin = _kinematic(xf)
                if di <= _STEER_EPS_DEG and kin is None:
                    row.update({"straight_ahead": True, "turn_center": None,
                                "inner_wheel_radius_mm": None,
                                "outer_wheel_radius_mm": None,
                                "centerline_radius_mm": None,
                                "outer_swept_radius_mm": None,
                                "kinematic": None,
                                "turning_radius_mm": None,
                                "turning_radius_m": None,
                                "turning_radius_ft": None})
                    extremes.append(row)
                    continue
                di_r = math.radians(max(di, _STEER_EPS_DEG))
                # Closed-form Ackermann cross-check: the turn centre lies on the
                # non-steered axle line, offset laterally from the centreline toward the
                # inner wheel by wheelbase/tan(inner steer) + half track.
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
                radius = (kin["turning_radius_mm"] if kin else r_center)
                row.update({
                    "straight_ahead": False,
                    "turn_center": center,
                    "inner_wheel_radius_mm": r_inner,
                    "outer_wheel_radius_mm": r_outer,
                    "centerline_radius_mm": r_center,
                    "ackermann_outer_deg": ack_outer,
                    "ackermann_error_deg": (do - ack_outer) if do is not None else None,
                    "outer_swept_radius_mm": (kin["outer_swept_radius_mm"] if kin
                                              else swept),
                    "kinematic": kin,
                    "radius_source": "wheel axle lines" if kin else "closed-form Ackermann",
                    "turning_radius_mm": radius,
                    "turning_radius_m": radius / 1000.0,
                    "turning_radius_ft": radius / _MM_PER_FT})
                extremes.append(row)

            turning = [r for r in extremes if not r["straight_ahead"]]
            if not turning:
                warnings.append("neither extreme steers; the reported sweep is straight "
                                "ahead at both ends")
            tightest = (min(turning, key=lambda r: r["turning_radius_mm"])
                        if turning else None)
            interference = out.get("interference") or {}
            first_val = interference.get("first_collision_value")
            unit = (out.get("driver") or {}).get("unit")
            result = {
                "ok": True,
                "doc_id": d,
                "mate_id": out.get("mate_id"),
                "driver": out.get("driver"),
                "wheelbase_mm": wheelbase_mm,
                "track_mm": track_mm,
                "derived_from": derived_from,
                "steered_components": [name_of.get(c, c) for c in steered],
                "second_axle_components": [name_of.get(c, c) for c in rear_ids],
                "wheel_of": {name_of.get(k, k): name_of.get(w, w)
                             for k, w in wheel_of.items()},
                "turning_radius_mm": tightest["turning_radius_mm"] if tightest else None,
                "turning_radius_m": tightest["turning_radius_m"] if tightest else None,
                "turning_radius_ft": tightest["turning_radius_ft"] if tightest else None,
                "interference": interference,
                "first_interference_deg": (first_val if unit == "deg" else None),
                "extremes": extremes,
                "tightest": tightest,
                "assumptions": assumptions,
                "warnings": warnings,
            }
            if tightest:
                kin = tightest.get("kinematic") or {}
                result["summary"] = (
                    f"wheelbase {wheelbase_mm:.4g} mm, track {track_mm:.4g} mm | tightest "
                    f"turning radius {tightest['turning_radius_m']:.4g} m "
                    f"({tightest['turning_radius_ft']:.4g} ft) at "
                    f"{tightest['inner_steer_deg']:.4g} deg inner steer, outer swept "
                    f"{tightest['outer_swept_radius_mm']:.4g} mm"
                    + (f", axle-line residual {kin['axle_line_residual_mm']:.4g} mm"
                       if kin else " (closed-form Ackermann; no axle lines read)")
                    + (f"; first interference at {first_val:.4g} deg"
                       if isinstance(first_val, (int, float)) and unit == "deg" else ""))
            else:
                result["summary"] = "no steering at either sweep extreme"
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
