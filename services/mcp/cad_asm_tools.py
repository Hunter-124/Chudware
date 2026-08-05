"""CAD assembly + motion MCP tools (T-RF8 split of cad_tools.py): new_assembly, asm_add_component, asm_mate, solve_mates, simulate_motion, motion_fea_handoff, list_world_bodies."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import os

from .cad_common import (
    _CUR,
    _autosave,
    _autosave_all_open,
    _describe_type,
    _doc,
    _docs_in_version_series,
    _dumps,
    _err,
    _is_empty_slot_word,
    _merge_face_sets,
    _normalize_optional_slot,
    _persist_study,
    _pick_best_doc,
    _publish_current,
    _resolve_face_points,
    _resolve_material,
    _tmp,
    _version_series_base,
    cb,
    coerce_json_or_repr,
    require_list,
    sh,
)

def register_cad_asm_tools(mcp: FastMCP) -> None:
    """CAD assembly + motion MCP tools (T-RF8 split of cad_tools.py): new_assembly, asm_add_component, asm_mate, solve_mates, simulate_motion, motion_fea_handoff, list_world_bodies."""


    # ---------------------------------------------------------------- assembly

    # Component source docs opened for mate-query resolution, keyed by absolute path —
    # asm_mate resolves plain-word queries against the COMPONENT's own part document, and
    # reopening the same .step+ for every mate would be wasteful.
    _ASM_QUERY_DOCS: dict = {}



    def _asm_component_doc(path: str) -> str:
        """doc_id of the component's source part, opened (once) for geometry queries."""
        import os as _os
        p = _os.path.abspath(path)
        d = _ASM_QUERY_DOCS.get(p)
        if d:
            try:  # still open?
                cb.call("doc_tree", {"doc_id": d})
                return d
            except Exception:  # noqa: BLE001
                _ASM_QUERY_DOCS.pop(p, None)
        r = cb.call("doc_open", {"path": p})
        d = r.get("doc_id")
        _ASM_QUERY_DOCS[p] = d
        return d



    def _asm_components(doc_id: str) -> list:
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        comps = tr.get("components") if isinstance(tr, dict) else None
        return comps if isinstance(comps, list) else []



    def _asm_revision(doc_id: str) -> int:
        return cb.call("doc_tree", {"doc_id": doc_id}).get("revision", 0)



    @mcp.tool()
    def new_assembly(name: str = "assembly") -> str:
        """Create a new, empty ASSEMBLY document and make it current. Returns its doc_id.

        An assembly PLACES saved part files as components and positions them with MATES —
        the SolidWorks/Onshape model. Build the parts first (new_part/make_standard_part/
        import_part), save_part() each to a .step+ file, then:
          1. new_assembly("motor_mount")
          2. asm_add_component(path, fixed=True)      the grounded base component
          3. asm_add_component(path2), ...            the moving/attached components
          4. asm_mate("concentric", a=..., b=...)     seat components against each other
          5. solve_mates() -> describe_assembly() / check_interference() to validate fit.
        For a quick static stack-up WITHOUT mates, a multi-body part (import_part into= +
        transform features) also works — but only an assembly gives you mates, motion
        (simulate_motion) and per-component BOM identity.

        ONE DESIGN IS ONE DOCUMENT, exactly as for new_part: an assembly of that name that
        is already open is RESUMED, and so is the assembly this one is the next VERSION of
        ("motor_mount_v2" while "motor_mount" is open with components). Iterating by
        bumping the name gives the user a duplicate tab, a duplicate file, a duplicate PLM
        item and a "Recover unsaved parts?" row per round; the versions of one design live
        in its history (plm_timeline / plm_rollback). Reuse the name the reply hands back.
        """
        try:
            docs = [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                    if isinstance(e, dict) and e.get("doc_id")]
            series = _pick_best_doc(_docs_in_version_series(docs, name, kind="assembly"))
            if series is not None:
                did = series["doc_id"]
                _CUR["doc_id"] = did
                actual = series.get("name") or name
                return _dumps({
                    "ok": True, "doc_id": did, "name": actual, "kind": "assembly",
                    "revision": series.get("revision", 0),
                    "reused": True, "resumed": True, "resumed_by": "version_series",
                    "version_series": _version_series_base(name),
                    "requested_name": name,
                    "component_count": series.get("component_count"),
                    "note": f'"{name}" is the next VERSION of the open assembly '
                            f'"{actual}" (doc_id={did}), not a separate assembly — '
                            f"resumed it so the design stays ONE document with ONE "
                            f"version history (plm_timeline / plm_rollback). Edit it in "
                            f'place and keep calling it "{actual}".'})
            # resume_if_open makes the engine decide check-and-mint under its mint lock,
            # so two callers racing on one name get one document (the same contract
            # new_part relies on — see cad_doc_tools.new_part and core/doc/session.cpp).
            r = cb.call("doc_new", {"kind": "assembly", "name": name,
                                    "resume_if_open": True})
            did = r.get("doc_id")
            _CUR["doc_id"] = did
            # doc_new UNIQUIFIES a colliding name; report what the document is really
            # called, never the ask, or every later name-based lookup repeats the lie.
            out = {"ok": True, "doc_id": did, "name": r.get("name") or name,
                   "kind": "assembly", "revision": r.get("revision", 0)}
            if r.get("reused"):
                out.update(reused=True, resumed=True,
                           component_count=r.get("component_count"),
                           note=f'resumed the open assembly "{out["name"]}" '
                                f"(doc_id={did}) instead of minting a duplicate document")
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def asm_add_component(path: str, name: Optional[str] = None, fixed: Optional[bool] = None,
                          at: Optional[list] = None, rotate: Optional[list] = None,
                          angle: float = 0.0, doc_id: Optional[str] = None) -> str:
        """Insert a saved part file into the current assembly as a component.

        path  : the part file to place (.step+ preferred — editable; plain STEP accepted).
        name  : component display name (default: the file stem).
        fixed : ground this component (default: the FIRST component is fixed, later ones free).
        at    : optional [x,y,z] mm initial translation (a starting pose for the solver, or
                the final pose if you skip mates entirely).
        rotate/angle : optional axis [ax,ay,az] + degrees initial rotation (applied before
                `at`'s translation).
        Returns the new component id ("k1", "k2", ...) for use in asm_mate refs.
        """
        try:
            import math as _math
            import os as _os
            d = _doc(doc_id)
            p = _os.path.abspath(path)
            if not _os.path.exists(p):
                return _dumps({"ok": False, "error": f"no such file: {p}"})
            op: dict = {"op": "asm_insert_component", "source": {"path": p}}
            if name:
                op["name"] = name
            if fixed is not None:
                op["fixed"] = bool(fixed)
            if at is not None or rotate is not None:
                # row-major 4x4: rotation about `rotate` by `angle` deg, then translate `at`.
                t = [0.0, 0.0, 0.0] if at is None else [float(v) for v in at]
                r3 = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
                if rotate is not None and angle:
                    ax = [float(v) for v in rotate]
                    n = _math.sqrt(sum(v * v for v in ax)) or 1.0
                    x, y, z = (v / n for v in ax)
                    c, s = _math.cos(_math.radians(angle)), _math.sin(_math.radians(angle))
                    C = 1 - c
                    r3 = [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                          [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                          [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]
                op["transform"] = [r3[0][0], r3[0][1], r3[0][2], t[0],
                                   r3[1][0], r3[1][1], r3[1][2], t[1],
                                   r3[2][0], r3[2][1], r3[2][2], t[2],
                                   0.0, 0.0, 0.0, 1.0]
            r = cb.call("doc_apply_ops", {"doc_id": d, "base_revision": _asm_revision(d),
                                          "with_tess": False, "ops": [op]})
            kid = (r.get("id_map") or {}).get("$0")
            comps = _asm_components(d)
            me = next((c for c in comps if c.get("id") == kid), {})
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "component": kid,
                           "name": me.get("name"), "fixed": me.get("fixed"),
                           "component_count": len(comps)})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    def _normalize_mate_operand(tag: str, raw) -> dict:
        """Reshape asm_mate's `a`/`b` operand into {"component": "kN", "query": ...}.

        Besides the canonical dict, accepts: a one-element list wrapping it (the same
        list-vs-object confusion cad_doc_tools' refs.base/refs.tool fix normalizes for
        boolean operands); a bare component-id string ("k1" instead of {"component":"k1"} —
        `query` then defaults to "top" exactly as it already does when the dict omits it);
        and the {"kind":"component","id":"k1",...} selector shape models reach for because
        OTHER ref slots use it. A multi-item list or a genuinely empty/placeholder value
        raises a teaching ValueError instead of guessing which component was meant.
        """
        v = coerce_json_or_repr(raw)
        if isinstance(v, list):
            if len(v) != 1:
                raise ValueError(
                    f"{tag} must name ONE component ({{'component': 'kN', 'query': ...}}, "
                    f"or a bare component id) — got a {len(v)}-item list. asm_mate mates "
                    f"exactly two single component faces per call.")
            v = coerce_json_or_repr(v[0])
        if isinstance(v, str):
            s = v.strip()
            if not s or _is_empty_slot_word(s):
                raise ValueError(
                    f"{tag} is empty — asm_mate needs a component on both sides "
                    f"({{'component': 'kN', 'query': ...}} or a bare component id string).")
            return {"component": s}
        if isinstance(v, dict):
            if "component" not in v and v.get("kind") in ("component", "part") and v.get("id"):
                out = {"component": v["id"]}
                if "query" in v:
                    out["query"] = v["query"]
                return out
            if "component" not in v:
                raise ValueError(
                    f"{tag} must be {{'component': 'kN', 'query': ...}}, got a dict "
                    f"with keys {sorted(v)} (missing 'component').")
            return v
        raise ValueError(f"{tag} must be {{'component': 'kN', 'query': ...}}, got "
                          f"{_describe_type(raw)}")


    @mcp.tool()
    def asm_mate(mate_type: str, a: Union[dict, str, list], b: Union[dict, str, list],
                 value: Optional[float] = None,
                 align: str = "aligned", solve: bool = True,
                 doc_id: Optional[str] = None) -> str:
        """Add a MATE between two components of the current assembly (and solve it).

        mate_type : coincident | parallel | perpendicular | tangent | concentric |
                    distance | angle | lock  (distance mm / angle deg carry `value`).
        a, b      : each names ONE face/edge of ONE component:
                      {"component": "k1", "query": {"of":"face","side":"top"}}
                    `query` is the same plain-word geometry query used everywhere
                    (query_geometry), resolved against that component's SOURCE part — a bare
                    side word ("top") also works. It must resolve to exactly ONE face/edge;
                    narrow with pick/near/count if it matches several.
        align     : "aligned" or "anti" (flip which way the normals/axes face).
        value     : the driving dimension for distance/angle mates.
        solve     : re-solve the mate network immediately (default) and report status.

        Typical seating pattern: `concentric` (bolt shank ↔ hole bore) + `coincident`
        (head seat face ↔ plate face). Returns the mate id (drive it with simulate_motion
        for kinematics) plus the solve status and each component's world bbox.
        """
        try:
            d = _doc(doc_id)
            comps = {c.get("id"): c for c in _asm_components(d)}
            if not comps:
                return _dumps({"ok": False, "error": "assembly has no components — "
                               "asm_add_component first (or is this a part doc?)"})
            # LIVE-DIALECT (run-97813015264134 pattern): a model can send `a`/`b` as a
            # stringified dict ("{'component': 'k1', ...}"), a bare component-id string, a
            # one-element list, or the {"kind":"component","id":...} selector shape other ref
            # slots use — _normalize_mate_operand reshapes every equivalent form; only a
            # genuinely unresolvable value raises.
            refs = []
            for tag, raw in (("a", a), ("b", b)):
                try:
                    ref = _normalize_mate_operand(tag, raw)
                except ValueError as e:
                    return _dumps({"ok": False, "error": str(e)})
                kid = ref["component"]
                if kid not in comps:
                    return _dumps({"ok": False, "error": f"{tag}: no component '{kid}' "
                                   f"(have: {sorted(comps)})"})
                src = ((comps[kid].get("source") or {}).get("path")) or ""
                if not src:
                    return _dumps({"ok": False, "error": f"{tag}: component '{kid}' has no "
                                   "source path (embedded components not supported yet)"})
                q = ref.get("query", "top")
                if isinstance(q, str):
                    q = {"of": "face", "side": q}
                part_doc = _asm_component_doc(src)
                matches = (cb.call("doc_query", {"doc_id": part_doc, "query": q})
                           .get("matches") or [])
                if len(matches) != 1:
                    brief = [{k: m.get(k) for k in ("sid", "kind", "surf", "side",
                                                    "centroid", "radius")}
                             for m in matches[:8]]
                    return _dumps({"ok": False, "error":
                                   f"{tag}: query must resolve to exactly ONE face/edge of "
                                   f"'{kid}' — got {len(matches)}. Narrow with pick/near/"
                                   "count (candidates below).", "candidates": brief})
                refs.append({"component": kid, "selector": matches[0]})
            mate: dict = {"type": mate_type, "align": align, "suppressed": False,
                          "refs": refs}
            if value is not None:
                mate["value"] = {"value": float(value)}
            r = cb.call("doc_apply_ops", {"doc_id": d, "base_revision": _asm_revision(d),
                                          "with_tess": False,
                                          "ops": [{"op": "asm_add_mate", "mate": mate}]})
            mate_id = (r.get("id_map") or {}).get("$0")
            out: dict = {"ok": True, "doc_id": d, "mate": mate_id, "type": mate_type}
            if solve:
                sr = cb.call("doc_solve_mates", {"doc_id": d})
                out["solve_status"] = (sr.get("rebuild") or {}).get("doc_status")
                wb = cb.call("asm_world_bodies", {"doc_id": d})
                out["world_bodies"] = wb.get("bodies")
            _publish_current(d)
            _autosave(d)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def solve_mates(doc_id: Optional[str] = None) -> str:
        """Solve the mate constraints of an ASSEMBLY document (positions its components).

        Only valid on an assembly doc (e.g. one opened from a .step+ assembly via import_part) —
        on a single part it returns a clear "not an assembly" error. Solves the mate network and
        returns the rebuilt assembly state. Follow with list_world_bodies to read where each
        component landed.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_solve_mates", {"doc_id": d})
            _publish_current(d)
            return _dumps({"ok": True, "doc_id": d, "revision": r.get("revision"),
                           "status": (r.get("rebuild") or {}).get("doc_status")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # Mate types whose `value` is a driven dimension we can sweep to MOVE the mechanism.
    # (revolute/prismatic freedom is provided by concentric/coincident/slot mates that have
    # no value; you drive the DIMENSIONAL mate that parameterizes that freedom.)
    _DRIVABLE_MATE_TYPES = {
        "angle": "deg", "angle_limit": "deg",
        "distance": "mm", "distance_limit": "mm",
        "gear": "ratio", "rack_pinion": "mm", "screw": "mm",
    }



    def _asm_mates(doc_id: str) -> list:
        """The doc's non-suppressed mate dicts (from doc_tree)."""
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        mates = tr.get("mates") if isinstance(tr, dict) else None
        return mates if isinstance(mates, list) else []



    def _asm_body_transforms(doc_id: str) -> list:
        """Per-component world 4x4 transform (row-major 16) after the current solve.

        Read from doc_tree's components[].transform — the pose doc_solve_mates writes back
        (NOT asm_world_bodies, which only returns bboxes; a bbox cannot orient a body for
        animation, so the trajectory carries the full transform instead)."""
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        comps = tr.get("components") if isinstance(tr, dict) else None
        out = []
        for c in (comps or []):
            if c.get("suppressed"):
                continue
            out.append({"component": c.get("id"),
                        "name": c.get("name") or c.get("id"),
                        "transform": c.get("transform")})
        return out



    @mcp.tool()
    def simulate_motion(mate_id: Optional[str] = None, start: Optional[float] = None,
                        stop: Optional[float] = None, steps: int = 24,
                        check_interference: bool = True, min_clearance_mm: float = 0.0,
                        loop: bool = False, dynamics: Optional[dict] = None,
                        doc_id: Optional[str] = None) -> str:
        """Kinematic motion study: DRIVE one mate over a range, re-solve the mechanism each
        step, return the trajectory (every body's pose per step) + a swept interference check.

        This is the "does this linkage / cam / gear-train clear everything as it moves?" tool —
        SolidWorks Motion / Onshape Assembly-Motion for the common kinematic case. It drives ONE
        DIMENSIONAL mate (its `value`) across a sweep and re-solves the whole mate graph at each
        sample, so the rest of the mechanism follows. Positions carry forward step-to-step (the
        previous solved pose warm-starts the next), so a full-cycle sweep stays on one branch
        instead of flipping to a mirror configuration.

        Scope: kinematics by default; pass `dynamics` (even {}) to ALSO run rigid-body inverse
        dynamics over the swept trajectory — mass/inertia from each component's geometry x
        density, gravity, applied forces/torques and simple viscous damping, with per-mate JOINT
        REACTION forces/moments solved through the mate constraint jacobian. Contact dynamics is
        still out of scope.

        dynamics : None (default) = kinematics only, result unchanged. A dict (may be empty —
                  all keys optional) turns on dynamics and adds a `dynamics` block:
                  {duration_s (sweep wall-time, default 1.0), gravity:[3] m/s² (default
                  [0,0,-9.81]), default_density_g_cm3 (default 7.85 steel),
                  densities:{component_id: g_cm3}, masses:{component_id: kg},
                  loads:[{component, force_n:[3], torque_nm:[3]}],
                  damping:{linear_ns_per_m, angular_nms_per_rad}}.
                  The block carries per-frame per-mate reactions ({mate_id, force_n:[3] N,
                  moment_nm:[3] N·m, force_mag_n}), per_joint_worst, worst_case and warnings —
                  feed it to motion_fea_handoff to stress the worst-loaded link. (`loop` extra
                  samples are not part of the dynamics timeline.)

        mate_id : which mate to drive. Must be a DIMENSIONAL mate whose value means motion —
                  one of angle / angle_limit (deg), distance / distance_limit (mm),
                  gear / rack_pinion / screw. A revolute/concentric/slot mate has no value to
                  sweep: drive the angle/distance mate that dimensions that joint instead. Omit
                  to have the tool pick the single drivable mate if there is exactly one.
        start,stop : the value range to sweep (in the mate's unit — deg or mm). Omit `start` to
                  begin at the mate's current value. `stop` is required.
        steps   : number of SAMPLES across [start, stop] inclusive (>=2). More = finer/smoother
                  and safer against branch flips on tight mechanisms.
        loop    : if True, append a final sample back at `start` (close the cycle, e.g. a crank
                  0->360->0) so playback loops seamlessly.
        check_interference : run check_interference (eval_interference) at EACH step and flag the
                  FIRST step whose bodies clash. Reuses the existing interference engine — this
                  tool never reimplements collision. min_clearance_mm is forwarded to it.

        Returns {ok, doc_id, mate_id, driver:{type,unit,start,stop,steps,samples},
                 trajectory:[{i, value, dof, converged, bodies:[{component,name,transform}]}],
                 interference:{checked, first_collision_step, first_collision_value, detail},
                 summary}. Each `transform` is the body's world 4x4 (row-major 16) at that sample
        — the per-step per-body pose a viewer needs to animate the mechanism (the GUI-playback
        seam; see cad_guide('motion')). The driven mate is RESTORED to its original value when
        the study finishes (a study reads motion; it does not leave the assembly moved).
        """
        try:
            d = _doc(doc_id)
            mates = _asm_mates(d)
            if not mates:
                raise ValueError("simulate_motion: document has no mates (not an assembly, or "
                                 "no mates added). Build/open an assembly with mates first.")
            drivable = [m for m in mates
                        if m.get("type") in _DRIVABLE_MATE_TYPES and not m.get("suppressed")]
            # LIVE-DIALECT pattern (refs.faces:"none" analogue): a model saying mate_id="" /
            # "none" MEANS "no specific mate — auto-pick", the same as omitting the arg. A
            # literal lookup for a mate named "none" always fails, so normalize first.
            mate_id = _normalize_optional_slot(mate_id)
            if mate_id is None:
                if len(drivable) == 1:
                    mate = drivable[0]
                    mate_id = mate.get("id")
                elif not drivable:
                    raise ValueError("simulate_motion: no drivable (dimensional) mate found. "
                                     "Drivable types: " + ", ".join(sorted(_DRIVABLE_MATE_TYPES))
                                     + ". Add e.g. an angle mate to drive the joint.")
                else:
                    ids = ", ".join(f"{m.get('id')}({m.get('type')})" for m in drivable)
                    raise ValueError("simulate_motion: multiple drivable mates — pass mate_id. "
                                     f"Candidates: {ids}")
            else:
                mate = next((m for m in mates if m.get("id") == mate_id), None)
                if mate is None:
                    raise ValueError(f"simulate_motion: mate '{mate_id}' not found.")
                if mate.get("type") not in _DRIVABLE_MATE_TYPES:
                    raise ValueError(
                        f"simulate_motion: mate '{mate_id}' is a '{mate.get('type')}' mate with "
                        "no driven value to sweep. Drive a dimensional mate "
                        "(" + ", ".join(sorted(_DRIVABLE_MATE_TYPES)) + ") instead.")
            mtype = mate.get("type")
            unit = _DRIVABLE_MATE_TYPES[mtype]

            def _cur_value(m: dict) -> float:
                v = m.get("value")
                if isinstance(v, dict):
                    return float(v.get("value", 0.0))
                if isinstance(v, (int, float)):
                    return float(v)
                return 0.0

            orig_value = _cur_value(mate)
            if start is None:
                start = orig_value
            if stop is None:
                raise ValueError("simulate_motion: `stop` is required (the end of the sweep).")
            steps = int(steps)
            if steps < 2:
                raise ValueError("simulate_motion: `steps` must be >= 2.")

            samples = [start + (stop - start) * i / (steps - 1) for i in range(steps)]
            if loop:
                samples.append(start)

            def _set_and_solve(val: float) -> dict:
                cb.call("doc_apply_ops", {
                    "doc_id": d,
                    "base_revision": cb.call("doc_tree", {"doc_id": d}).get("revision", 0),
                    "ops": [{"op": "asm_edit_mate", "id": mate_id, "value": {"value": float(val)}}],
                    "with_tess": False})
                return cb.call("doc_solve_mates", {"doc_id": d})

            trajectory = []
            first_collision_step = None
            first_collision_value = None
            first_collision_detail = None
            try:
                for i, val in enumerate(samples):
                    sm = _set_and_solve(val)
                    status = (sm.get("rebuild") or {}).get("doc_status") or {}
                    mate_stat = (sm.get("rebuild") or {}).get("mates")
                    converged = True
                    dof = None
                    if isinstance(status, dict):
                        dof = status.get("dof")
                    if isinstance(mate_stat, list):
                        converged = all(s.get("status") in ("ok", "redundant")
                                        for s in mate_stat)
                    frame = {"i": i, "value": round(val, 9),
                             "converged": bool(converged),
                             "bodies": _asm_body_transforms(d)}
                    if dof is not None:
                        frame["dof"] = dof
                    if check_interference:
                        clash = cb.call("eval_interference",
                                        {"doc_id": d, "include_coincident": False,
                                         "min_clearance_mm": float(min_clearance_mm)})
                        inters = clash.get("interferences") or []
                        frame["interference"] = bool(inters)
                        if inters and first_collision_step is None:
                            first_collision_step = i
                            first_collision_value = round(val, 9)
                            first_collision_detail = inters
                    trajectory.append(frame)
            finally:
                # ALWAYS restore the mechanism to where it started — a study is non-destructive.
                try:
                    _set_and_solve(orig_value)
                except Exception:  # noqa: BLE001
                    pass

            _publish_current(d)

            if check_interference and first_collision_step is not None:
                summary = (f"drove {mtype} mate '{mate_id}' {start:g}->{stop:g} {unit} over "
                           f"{len(samples)} samples; INTERFERENCE first at step "
                           f"{first_collision_step} (value {first_collision_value:g} {unit}).")
            elif check_interference:
                summary = (f"drove {mtype} mate '{mate_id}' {start:g}->{stop:g} {unit} over "
                           f"{len(samples)} samples; no interference across the sweep.")
            else:
                summary = (f"drove {mtype} mate '{mate_id}' {start:g}->{stop:g} {unit} over "
                           f"{len(samples)} samples (interference check skipped).")

            out = {
                "ok": True, "doc_id": d, "mate_id": mate_id,
                "driver": {"type": mtype, "unit": unit, "start": start, "stop": stop,
                           "steps": steps, "samples": len(samples), "loop": bool(loop)},
                "trajectory": trajectory,
                "interference": {"checked": bool(check_interference),
                                 "first_collision_step": first_collision_step,
                                 "first_collision_value": first_collision_value,
                                 "detail": first_collision_detail},
                "summary": summary,
            }
            # T-FIN20: rigid-body inverse dynamics on the SAME sweep (engine-side).
            # Kinematics-only calls never reach this branch — their result shape is
            # regression-frozen above.
            if dynamics is not None:
                # LIVE-DIALECT guard: a stringified `dynamics` dict used to fail the bare
                # isinstance() check and get silently replaced with {} — turning dynamics ON
                # but discarding every config the model actually sent, with no error at all
                # (the class-5 "reports success but did the wrong thing" failure). Coerce first.
                dyn = coerce_json_or_repr(dynamics)
                r_dyn = cb.call("simulate_motion", {
                    "doc_id": d, "mate_id": mate_id, "start": float(start),
                    "stop": float(stop), "steps": int(steps),
                    "check_interference": False, "min_clearance_mm": 0.0,
                    "dynamics": dyn if isinstance(dyn, dict) else {}})
                out["dynamics"] = r_dyn.get("dynamics") or {
                    "warnings": ["engine returned no dynamics block"]}
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def motion_fea_handoff(component: str, fixtures: list,
                           mate_id: Optional[str] = None,
                           start: Optional[float] = None, stop: Optional[float] = None,
                           steps: int = 24, at: Union[str, float] = "worst",
                           dynamics: Optional[dict] = None,
                           material: Union[str, dict] = "steel",
                           mesh_fineness: float = 0.5, dry_run: bool = False,
                           doc_id: Optional[str] = None) -> str:
        """Motion → static FEA handoff: "is this link strong enough at the worst instant?"
        in one call (aicad.simhandoff/1, coupling `motion_to_fea`).

        Pipeline: (1) runs the motion study WITH rigid-body dynamics (simulate_motion +
        dynamics — mass/inertia from geometry x density, gravity, joint reactions via the
        mate constraint jacobian); (2) picks the WORST-CASE frame (or the frame nearest a
        time you pass in `at`) for the joints touching the selected component; (3) opens
        that component's part file and applies each joint reaction as a FORCE on the mate
        face (load placed at the joint face via the mate ref's stored face centroid);
        (4) runs run_fea_static on it. The result embeds a validated handoff envelope
        under `simhandoff` with one condition per reaction force/moment, `case`-labelled
        with the chosen frame/time, full provenance, and the FEA study records the
        handoff id — closing the chain motion study → handoff → static study.

        component : the assembly component to stress-check — its id ("k2") or name ("Arm").
        fixtures  : face queries held fixed on THAT COMPONENT's part (run_fea_static
                    vocabulary, e.g. ["bottom"] or [{"of":"face","near":[x,y,z]}]).
                    Fix the face(s) of the joint you consider grounded; the OTHER joints'
                    reactions are applied as loads.
        mate_id / start / stop / steps : the motion sweep (same semantics as
                    simulate_motion; stop required, start defaults to current value).
        at        : "worst" (default) = frame with the peak reaction force magnitude on
                    this component's joints; or a time in seconds (float) to pick the
                    nearest frame.
        dynamics  : simulate_motion dynamics options (duration_s, gravity, densities,
                    masses, loads, damping). Defaults apply when omitted.
        dry_run   : if True, build the loads + envelope but SKIP the FEA solve (fast
                    inspection / no solver required).

        Returns {ok, doc_id (assembly), part_doc_id, frame:{i, t_s}, loads_applied,
        fea (run_fea_static summary or null when dry_run), simhandoff}. Moment reactions
        are recorded in the envelope (units N*m) but only forces are applied to the
        static study (point-moment loads are not supported by the solver — a warning is
        attached when a significant moment is dropped).
        """
        try:
            d = _doc(doc_id)
            # LIVE-DIALECT guard: `fixtures` is iterated bare (`for q in fixtures`) below —
            # a stringified list ("[{'of': 'face', ...}]") would silently iterate its
            # CHARACTERS instead of raising cleanly. Coerce/type-check up front.
            fixtures = require_list(fixtures, "fixtures",
                                     'a list of face queries, e.g. ["bottom"] or '
                                     '[{"of":"face","near":[x,y,z]}]')
            # Same "no specific mate" placeholder as simulate_motion (mate_id="" / "none" /
            # "null") — normalize BEFORE it's used to pick `start`'s default below, or the
            # literal-string lookup silently misses and `start` is computed from the wrong
            # (or no) mate while the later simulate_motion call auto-picks a different one.
            mate_id = _normalize_optional_slot(mate_id)
            if stop is None:
                raise ValueError("motion_fea_handoff: `stop` is required (end of the sweep).")
            mates = _asm_mates(d)
            if not mates:
                raise ValueError("motion_fea_handoff: document has no mates.")

            # --- resolve the component (id or name) --------------------------
            tr = cb.call("doc_tree", {"doc_id": d}).get("tree") or {}
            comps = [c for c in (tr.get("components") or []) if not c.get("suppressed")]
            comp = next((c for c in comps if c.get("id") == component or
                         (c.get("name") or "") == component), None)
            if comp is None:
                ids = ", ".join(f"{c.get('id')}({c.get('name')})" for c in comps)
                raise ValueError(f"motion_fea_handoff: component '{component}' not found. "
                                 f"Components: {ids}")
            cid = comp.get("id")
            src_path = (comp.get("source") or {}).get("path") or ""
            if not src_path or not os.path.exists(src_path):
                raise ValueError(f"motion_fea_handoff: component '{cid}' has no readable "
                                 f"source file ({src_path!r}) to run FEA on.")

            # --- default `start` to the driven mate's current value ------------
            if start is None:
                cand = [m for m in mates if m.get("id") == mate_id] if mate_id else \
                       [m for m in mates
                        if m.get("type") in _DRIVABLE_MATE_TYPES and not m.get("suppressed")]
                v = (cand[0].get("value") if cand else None)
                start = float(v.get("value", 0.0)) if isinstance(v, dict) else \
                        float(v) if isinstance(v, (int, float)) else 0.0

            # --- motion study WITH dynamics (engine-side sweep) ---------------
            dyn_opts = dict(dynamics) if isinstance(dynamics, dict) else {}
            r_mot = cb.call("simulate_motion", {
                "doc_id": d, "mate_id": mate_id or "", "start": float(start),
                "stop": float(stop), "steps": int(steps),
                "check_interference": False, "min_clearance_mm": 0.0,
                "dynamics": dyn_opts})
            dyn = r_mot.get("dynamics") or {}
            frames = dyn.get("frames") or []
            if not frames:
                return _dumps({"ok": False, "error": "motion dynamics produced no frames",
                               "warnings": dyn.get("warnings")})
            drv_mate = r_mot.get("mate_id")

            # --- joints touching this component -------------------------------
            touch_mids = set()
            sel_by_mid = {}
            for m in mates:
                for ref in (m.get("refs") or []):
                    if isinstance(ref, dict) and ref.get("component") == cid:
                        touch_mids.add(m.get("id"))
                        sel_by_mid[m.get("id")] = ref.get("selector") or {}
            have = {r.get("mate_id") for r in frames[0].get("reactions", [])}
            touch_mids &= have
            if not touch_mids:
                return _dumps({"ok": False, "error":
                               f"no solved joint reactions touch component '{cid}' — its "
                               "mates may be dangling (see dynamics.warnings).",
                               "warnings": dyn.get("warnings")})

            # --- pick the frame ------------------------------------------------
            def _peak(fr: dict) -> float:
                return max((r.get("force_mag_n", 0.0) for r in fr.get("reactions", [])
                            if r.get("mate_id") in touch_mids), default=0.0)
            if isinstance(at, str) and at == "worst":
                frame = max(frames, key=_peak)
            else:
                t_want = float(at)
                frame = min(frames, key=lambda fr: abs(fr.get("t_s", 0.0) - t_want))
            fi, ft = int(frame.get("i", 0)), float(frame.get("t_s", 0.0))
            case = f"frame{fi}@t{ft:.6g}s"

            # --- build loads + envelope conditions ------------------------------
            conditions = []
            loads = []
            warnings = list(dyn.get("warnings") or [])
            for r in frame.get("reactions", []):
                mid = r.get("mate_id")
                if mid not in touch_mids:
                    continue
                f_vec = [float(x) for x in (r.get("force_n") or [0, 0, 0])]
                m_vec = [float(x) for x in (r.get("moment_nm") or [0, 0, 0])]
                centroid = (sel_by_mid.get(mid) or {}).get("centroid")
                applied_to = (f"{cid}:face@{centroid}" if centroid else cid)
                prov = (f"simulate_motion dynamics frame {fi} (t={ft:.6g}s) joint '{mid}' "
                        f"reaction on component '{cid}'")
                conditions.append(sh.condition(
                    f"{mid}_reaction_force", "force", f_vec, "N", prov,
                    applied_to=applied_to, case=case))
                if any(abs(x) > 1e-9 for x in m_vec):
                    conditions.append(sh.condition(
                        f"{mid}_reaction_moment", "moment", m_vec, "N*m", prov,
                        applied_to=applied_to, case=case))
                    if max(abs(x) for x in m_vec) > 1e-3:
                        warnings.append(
                            f"joint '{mid}' carries a moment {m_vec} N*m that is NOT "
                            "applied to the static study (force loads only)")
                if centroid:
                    loads.append({"face": {"of": "face", "near": centroid}, "force": f_vec,
                                  "mate_id": mid})
                else:
                    warnings.append(f"mate '{mid}' ref has no stored face centroid — its "
                                    "reaction force was not applied as an FEA load")
            if not loads:
                return _dumps({"ok": False, "error":
                               "no reaction could be placed on a face of the component "
                               "(mate refs carry no face centroids)",
                               "warnings": warnings})

            # --- open the component part ----------------------------------------
            part_doc = cb.call("doc_open", {"path": src_path}).get("doc_id")

            # --- envelope (aicad.simhandoff/1) ------------------------------------
            env = sh.build_handoff(
                "motion_to_fea",
                sh.study_ref("motion", "simulate_motion", doc_id=d,
                             result_digest={"driver_mate": drv_mate,
                                            "worst_case": dyn.get("worst_case"),
                                            "picked_frame": {"i": fi, "t_s": ft}}),
                sh.study_ref("fea", "run_fea_static", doc_id=part_doc),
                geometry=[sh.geometry_ref("component", "doc_id", doc_id=part_doc,
                                          path=src_path, units="mm",
                                          derived_by="assembly component source",
                                          meta={"component": cid,
                                                "name": comp.get("name")})],
                conditions=conditions,
                warnings=warnings or None,
                meta={"at": at if isinstance(at, str) else float(at),
                      "case": case, "material": material if isinstance(material, str)
                      else "custom"})

            result = {"ok": True, "doc_id": d, "component": cid,
                      "part_doc_id": part_doc, "frame": {"i": fi, "t_s": ft},
                      "loads_applied": loads, "warnings": warnings, "fea": None}
            if dry_run:
                result["note"] = "dry_run: loads + envelope built, FEA solve skipped"
                return _dumps(sh.attach(result, env))

            # --- static FEA on the component (same path as run_fea_static) --------
            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": part_doc, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(part_doc, q))
            if not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved on the "
                               "component part — check the fixture queries."})
            load_specs = []
            for ld in loads:
                faces = _resolve_face_points(part_doc, ld["face"])
                load_specs.append({"faces": _merge_face_sets(faces), "type": "force",
                                   "vector": ld["force"]})
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "loads": load_specs, "mesh_order": 2, "fineness": mesh_fineness}
            _autosave_all_open()
            res = cb.call("sim_run_static", {"study": study})
            if not res.get("ok"):
                result["fea"] = {"ok": False, "error": res.get("error", "solve failed")}
                return _dumps(sh.attach(result, env))
            _persist_study(part_doc, {
                "type": "fea_static",
                "name": f"Motion reactions {case} ({mat.get('name', material)})",
                "engine": "cpp", "material": mat, "fixtures": fixtures,
                "loads": [{"face": ld["face"], "force": ld["force"]} for ld in loads],
                "mesh_fineness": mesh_fineness,
                "results": res.get("summary", {}), "n_dof": res.get("n_dof"),
                "simhandoff_id": env.get("id"),
            })
            result["fea"] = {"ok": True, "summary": res.get("summary", {}),
                             "n_dof": res.get("n_dof"), "material": mat.get("name", material)}
            return _dumps(sh.attach(result, env))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def list_world_bodies(explode: bool = False, doc_id: Optional[str] = None) -> str:
        """List a doc's bodies with their WORLD-placed bounding boxes.

        Works on an ASSEMBLY doc (each placed component after mates are solved) AND on a
        multi-body PART — e.g. one assembled via import_part + transform (where the bodies are
        already placed absolutely). Returns each body {name, component, bbox:{min,max,size}};
        use it to see where every part sits in the shared frame, or pair with check_interference
        to validate fit. explode=True applies the exploded-view offsets on an assembly (ignored
        on a plain multi-body part).
        """
        try:
            d = _doc(doc_id)
            r = cb.call("asm_world_bodies", {"doc_id": d, "explode": bool(explode)})
            bodies = r.get("bodies", [])
            if r.get("multibody_part"):
                return _dumps({"ok": True, "doc_id": d, "multibody_part": True,
                               "body_count": len(bodies), "bodies": bodies})
            return _dumps({"ok": True, "doc_id": d, "body_count": len(bodies),
                           "bodies": bodies})
        except Exception as e:  # noqa: BLE001
            return _err(e)
