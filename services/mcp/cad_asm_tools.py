"""CAD assembly + motion MCP tools (T-RF8 split of cad_tools.py): new_assembly, asm_add_component, asm_mate, solve_mates, simulate_motion, motion_fea_handoff, list_world_bodies."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import os
import copy
import math

from .cad_common import (
    _CUR,
    _autosave,
    _autosave_all_open,
    _describe_type,
    _doc,
    _docs_named,
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
    _resolve_one_selector,
    _resolve_material,
    _strip_version_suffix,
    _tmp,
    _version_series_base,
    cb,
    coerce_json_or_repr,
    note_placement_source,
    note_touched_doc,
    placement_source_of,
    placement_warnings,
    pose_to_transform16,
    require_list,
    sh,
)


def _solve_report(rebuild: dict) -> dict:
    """Read the mate solve's verdict out of a doc_apply_ops/doc_solve_mates rebuild.

    Returns {converged, worst_mate, worst_residual, tolerance, mates_over_defined,
    worst: [(id, type, status, residual) worst-first, <=5], message}. `message` is
    empty when the solve converged; otherwise it NAMES the mate holding it back and by
    how much, which is the one thing a caller needs to fix a diverging network — the
    engine used to report `doc_status.mates == "ok"` for a solve that never converged.
    """
    rb = rebuild if isinstance(rebuild, dict) else {}
    mates = [m for m in (rb.get("mates") or []) if isinstance(m, dict)]
    ranked = sorted(mates, key=lambda m: -float(m.get("residual") or 0.0))[:5]
    worst = [{"id": m.get("id"), "type": m.get("type"), "status": m.get("status"),
              "residual": float(m.get("residual") or 0.0)} for m in ranked]
    converged = bool(rb.get("converged", True))
    worst_mate = rb.get("worst_mate") or (worst[0]["id"] if worst else None)
    worst_res = float(rb.get("max_residual") or 0.0)
    over = bool(rb.get("over_defined", False))
    msg = ""
    if not converged:
        msg = (f"mate solve did not converge: worst mate '{worst_mate}' "
               f"residual {worst_res:.6g} (tolerance {float(rb.get('tolerance') or 0.0):.3g}; "
               "units are mm for positional rows, dimensionless for direction rows). "
               "Components stay near their authored poses — the solver no longer moves "
               "an assembly it cannot solve. Fix or suppress that mate (check its align "
               "sense and that both refs are the intended faces/axes), then re-solve.")
        if over:
            msg += " The network is also OVER-DEFINED; drop the redundant mate first."
    return {"converged": converged, "worst_mate": worst_mate,
            "worst_residual": worst_res, "tolerance": rb.get("tolerance"),
            "mates_over_defined": over, "worst": worst, "message": msg}

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
                note_touched_doc(did)
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
            # Verbatim when an assembly of that exact name is already open — that is the
            # engine's resume_if_open match and it keys on the name as given, so stripping
            # the token would miss it and mint a duplicate (same guard as new_part).
            exact_open = [e for e in _docs_named(docs, name)
                          if (e.get("kind") or "part") == "assembly"]
            mint_name = name if exact_open else _strip_version_suffix(name)
            r = cb.call("doc_new", {"kind": "assembly", "name": mint_name,
                                    "resume_if_open": True})
            did = r.get("doc_id")
            _CUR["doc_id"] = did
            note_touched_doc(did)
            # doc_new UNIQUIFIES a colliding name; report what the document is really
            # called, never the ask, or every later name-based lookup repeats the lie.
            actual = r.get("name") or mint_name
            out = {"ok": True, "doc_id": did, "name": actual,
                   "kind": "assembly", "revision": r.get("revision", 0)}
            if not r.get("reused") and mint_name != name:
                out["requested_name"] = name
                out["note"] = (
                    f'dropped the version token from "{name}" because revisions are '
                    f'tracked in PLM (plm_checkpoint / plm_timeline); this assembly was '
                    f'minted as "{actual}", so keep calling it "{actual}" from now on')
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
        at    : optional [x,y,z] mm translation — a STARTING POSE for the mate solver, or a
                MEASURED pose when you are placing without mates.
        rotate/angle : optional axis [ax,ay,az] + degrees rotation (applied BEFORE `at`'s
                translation).
        Returns the new component id ("k1", "k2", ...) for use in asm_mate refs, plus
        `placement_source` ("transform" when you passed at/rotate, "mate" once a mate on this
        component solves).

        `at`/`rotate` ARE NOT A GUESS. For INDEPENDENTLY-SOURCED parts (a wheel from one
        vendor onto a frame from another) a pose must come from ONE of:
          * an asm_mate (concentric/coincident/distance) — the solver derives the pose; or
          * a pose DERIVED FROM MEASURED REFERENCES: fit_cylinder_axis on each mating bore/
            shaft (axis point + direction + radius) and/or measure_between for the offsets,
            then the translation/rotation computed from those numbers.
        Either way FINISH with verification before you deliver: check_interference (no
        overlap, expected clearance) AND measure_between / list_world_bodies (the mating
        features really are coaxial/seated where you intended). A transform-placed component
        that nothing has re-checked since the last mutation makes save_part/export_part
        report `placement_unverified`. Never position a part by editing STEP/JSONL text.
        """
        try:
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
                # Same helper add_features' asm_edit_component re-pose path uses, so an
                # insert pose and a later re-pose can never disagree on the convention.
                op["transform"] = pose_to_transform16(at, rotate, angle)
            r = cb.call("doc_apply_ops", {"doc_id": d, "base_revision": _asm_revision(d),
                                          "with_tess": False, "ops": [op]})
            kid = (r.get("id_map") or {}).get("$0")
            comps = _asm_components(d)
            me = next((c for c in comps if c.get("id") == kid), {})
            # Placement provenance: a raw at/rotate pose is only as good as the measurement
            # behind it, so it is stamped "transform" and stays unverified until
            # check_interference runs. An identity insert is "origin" (nothing was asserted
            # about where it sits); asm_mate/solve_mates upgrade either to "mate".
            src = "transform" if (at is not None or rotate is not None) else "origin"
            note_placement_source(d, kid, src)
            _publish_current(d)
            _autosave(d)
            out = {"ok": True, "doc_id": d, "component": kid,
                   "name": me.get("name"), "fixed": me.get("fixed"),
                   "component_count": len(comps),
                   "placement_source": placement_source_of(d, kid)}
            if src == "transform":
                out["placement_note"] = (
                    "placed by transform — derive at/rotate from measured references "
                    "(fit_cylinder_axis / measure_between) or add an asm_mate, then verify "
                    "with check_interference + measure_between before exporting")
            return _dumps(out)
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
            # Both mated components now owe their pose to the solver, not to a raw transform.
            for ref in refs:
                note_placement_source(d, ref.get("component"), "mate")
            out: dict = {"ok": True, "doc_id": d, "mate": mate_id, "type": mate_type,
                         "placement_source": {ref["component"]: placement_source_of(
                             d, ref["component"]) for ref in refs}}
            if solve:
                sr = cb.call("doc_solve_mates", {"doc_id": d})
                rb = sr.get("rebuild") or {}
                out["solve_status"] = rb.get("doc_status")
                rep = _solve_report(rb)
                out["converged"] = rep["converged"]
                out["mate_residuals"] = rep["worst"]
                if not rep["converged"]:
                    # The mate landed in the doc; the NETWORK is what failed. Say which
                    # mate, so the next call fixes that one instead of rebuilding blind.
                    out["solve_error"] = rep["message"]
                    out["worst_mate"] = rep["worst_mate"]
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

        A solve that does NOT converge returns ok=False with `worst_mate`, `max_residual` and
        a `mate_residuals` table (worst five, mm for positional rows). The components stay
        near their authored poses in that case — the solver will not scatter an assembly it
        cannot solve — so fix or suppress the named mate and call again.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_solve_mates", {"doc_id": d})
            # Every component the mate network actually constrains is solver-placed now.
            for m in ((cb.call("doc_tree", {"doc_id": d}).get("tree") or {}).get("mates") or []):
                for ref in (m.get("refs") or []):
                    if isinstance(ref, dict):
                        note_placement_source(d, ref.get("component"), "mate")
            _publish_current(d)
            rb = r.get("rebuild") or {}
            rep = _solve_report(rb)
            out = {"ok": rep["converged"], "doc_id": d, "revision": r.get("revision"),
                   "status": rb.get("doc_status"), "converged": rep["converged"],
                   "dof": rb.get("dof"), "over_defined": rep["mates_over_defined"],
                   "max_residual": rep["worst_residual"], "tolerance": rep["tolerance"],
                   "mate_residuals": rep["worst"]}
            if not rep["converged"]:
                out["error"] = rep["message"]
                out["worst_mate"] = rep["worst_mate"]
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # Mate types whose `value` is a driven dimension we can sweep to MOVE the mechanism.
    # (revolute/prismatic freedom is provided by concentric/coincident/slot mates that have
    # no value; you drive the DIMENSIONAL mate that parameterizes that freedom.)
    _DRIVABLE_MATE_TYPES = {
        "angle": "deg",
        "distance": "mm",
    }



    def _asm_mates(doc_id: str) -> list:
        """The doc's non-suppressed mate dicts (from doc_tree)."""
        tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
        mates = tr.get("mates") if isinstance(tr, dict) else None
        return mates if isinstance(mates, list) else []






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
        REACTION forces/moments solved through the mate constraint jacobian. Tangent/cam mates
        contribute their instantaneous normal reaction only; unilateral lift-off/impact/contact
        integration remains out of scope.

        dynamics : None (default) = kinematics only. A dict (may be empty — all keys
                  optional) adds a `dynamics` block:
                  {duration_s (sweep wall-time, default 1.0), gravity:[3] m/s² (default
                  [0,0,-9.81]), default_density_g_cm3 (default 7.85 steel),
                  densities:{component_id: g/cm³}, masses:{component_id: kg},
                  loads:[{component, force_n:[3], torque_nm:[3]}],
                  damping:{linear_ns_per_m, angular_nms_per_rad}}.
                  The block reports ok/force_path_resolved plus per-frame mate reactions
                  ({mate_id, component_a, component_b, force_n:[3] N,
                  moment_nm:[3] N·m, force_mag_n, moment_mag_nm}), separate force and
                  moment residuals, worst-force/worst-moment summaries and warnings.
                  `loop`'s closing playback sample is not part of the dynamics timeline.

        mate_id : which mate to drive. The mate solver's scalar driven dimensions are
                  angle (deg) and distance (mm). Limit and mechanical-coupling mates store
                  bounds/ratio/pitch rather than a scalar target, so claiming their `value`
                  was drivable produced a stationary false-success sweep; drive an angle
                  or distance mate that parameterizes the mechanism instead. Omit to
                  auto-pick when exactly one drivable mate exists.
        start,stop : the value range to sweep (in the mate's unit — deg or mm). Omit `start` to
                  begin at the mate's current value. `stop` is required.
        steps   : number of SAMPLES across [start, stop] inclusive (>=2, >=3 with dynamics).
                  More samples improve finite-difference accuracy and branch continuity.
        loop    : if True, append the already-solved start pose as one final playback sample,
                  closing the cycle without changing the dynamics time map.
        check_interference : evaluate every sample and flag the FIRST solid overlap or
                  below-minimum-clearance pair. min_clearance_mm is in mm.

        Returns {ok, doc_id, mate_id, driver:{type,unit,start,stop,steps,samples},
                 trajectory:[{i, value, dof, converged, bodies:[{component,name,transform}]}],
                 interference:{checked, first_collision_step, first_collision_value, detail},
                 summary}. Each `transform` is the body's world 4x4 (row-major 16). ok=false
        distinguishes any unresolved/non-converged frame. The engine solves on an immutable
        assembly snapshot, so the live mate value/expression, component transforms, revision,
        undo history, dirty state and warm state never change.
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
                    raise ValueError(
                        "simulate_motion: no drivable (dimensional) mate found. "
                        "Drivable types: " + ", ".join(sorted(_DRIVABLE_MATE_TYPES))
                        + ". Add e.g. an angle mate to drive the joint — or, when the "
                        "components you want to move are positioned by concentric/"
                        "coincident mates about a measured axis (a steering upright on "
                        "its kingpin) and there is no dimension to add, drive them "
                        "directly with steering_turning_radius(steer={'components':[...],"
                        " 'axis':{'point':[x,y,z],'dir':[x,y,z]}, 'angle_deg':...}) or "
                        "sweep_envelope(steer=...), which rotate the named components "
                        "about that axis on a detached clone and re-solve the rest.")
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
                    v = v.get("value")
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    raise ValueError(
                        f"simulate_motion: mate '{mate_id}' has no numeric driven value.")
                value = float(v)
                if not math.isfinite(value):
                    raise ValueError(
                        f"simulate_motion: mate '{mate_id}' has a non-finite driven value.")
                return value

            if start is None:
                start = _cur_value(mate)
            if stop is None:
                raise ValueError("simulate_motion: `stop` is required (the end of the sweep).")
            start = float(start)
            stop = float(stop)
            if not math.isfinite(start) or not math.isfinite(stop):
                raise ValueError("simulate_motion: start and stop must be finite.")
            steps = int(steps)
            if steps < 2:
                raise ValueError("simulate_motion: `steps` must be >= 2.")
            min_clearance_mm = float(min_clearance_mm)
            if not math.isfinite(min_clearance_mm) or min_clearance_mm < 0.0:
                raise ValueError(
                    "simulate_motion: min_clearance_mm must be finite and non-negative.")

            payload = {
                "doc_id": d,
                "mate_id": mate_id,
                "start": start,
                "stop": stop,
                "steps": steps,
                "check_interference": bool(check_interference),
                "min_clearance_mm": min_clearance_mm,
            }
            if dynamics is not None:
                dyn = coerce_json_or_repr(dynamics)
                if not isinstance(dyn, dict):
                    raise ValueError("simulate_motion: dynamics must be an object/dict.")
                payload["dynamics"] = dyn

            # The engine solves on a private assembly snapshot: no apply/restore cycle,
            # revision churn, undo pollution, expression loss, or silent restore failure.
            out = cb.call("simulate_motion", payload)

            # Engine trajectory matrices are column-major for the native viewport.
            # This MCP tool's established public shape is row-major, so transpose the
            # representation (not the transform) at the seam.
            for frame in out.get("trajectory") or []:
                for body in frame.get("bodies") or []:
                    cm = body.get("transform")
                    if not isinstance(cm, list) or len(cm) != 16:
                        raise ValueError(
                            "simulate_motion: engine returned an invalid body transform.")
                    body["transform"] = [
                        cm[c * 4 + r] for r in range(4) for c in range(4)
                    ]

            trajectory = out.get("trajectory") or []
            if loop and trajectory:
                closing = copy.deepcopy(trajectory[0])
                closing["i"] = len(trajectory)
                closing["value"] = start
                trajectory.append(closing)
                out["trajectory"] = trajectory

            driver = out.setdefault("driver", {})
            driver.update({
                "type": mtype,
                "unit": unit,
                "start": start,
                "stop": stop,
                "steps": steps,
                "samples": len(trajectory),
                "loop": bool(loop),
            })
            if loop and out.get("summary"):
                out["summary"] += "; loop closure reuses the solved start pose"
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
                           doc_id: Optional[str] = None,
                           fixture_mate_id: Optional[str] = None,
                           body_load_face: Optional[Union[str, dict]] = None) -> str:
        """Motion → static FEA handoff for one isolated moving component.

        The fixed face must be the face of exactly one solved mate that joins the
        selected component to a fixed assembly component.  That mate's reaction is
        deliberately omitted from the FEA loads because the fixed boundary supplies
        it.  All other joint reactions are applied at their mate faces.

        Inverse dynamics also balances gravity, acceleration, damping and explicit
        body loads.  Their combined equivalent wrench is recovered from joint
        equilibrium and must be represented in the static study.  When its force is
        non-zero, ``body_load_face`` is required: it names one non-fixed face on which
        the equivalent force can be placed.  The face's line of action must reproduce
        the equivalent moment; otherwise this face-force-only solver cannot represent
        the snapshot and the handoff refuses instead of dropping a couple.

        ``fixture_mate_id`` may be omitted only when the fixture face identifies one
        grounded mate unambiguously.  ``fixture_mate_id`` and ``body_load_face`` are
        intentionally separate from ``mate_id`` (the driven mate) and ``fixtures``;
        no caller input is silently reinterpreted.

        Returns the applied local-N loads plus ``setup_diagnostics`` and
        ``balance_diagnostics``.  ``dry_run`` performs every setup/balance check and
        builds the handoff envelope but skips the FEA solve.
        """
        try:
            d = _doc(doc_id)
            fixtures = require_list(
                fixtures, "fixtures",
                'a one-item list naming the grounded mate face, e.g. ["bottom"] or '
                '[{"of":"face","near":[x,y,z]}]')
            if len(fixtures) != 1:
                raise ValueError(
                    "motion_fea_handoff: fixtures must resolve one grounded mate face; "
                    "multiple fixed faces cannot be associated with one omitted reaction.")
            mate_id = _normalize_optional_slot(mate_id)
            fixture_mate_id = _normalize_optional_slot(fixture_mate_id)
            if stop is None:
                raise ValueError("motion_fea_handoff: `stop` is required (end of the sweep).")
            stop = float(stop)
            if not math.isfinite(stop):
                raise ValueError("motion_fea_handoff: stop must be finite.")
            steps = int(steps)
            if steps < 3:
                raise ValueError(
                    "motion_fea_handoff: steps must be >= 3 for acceleration dynamics.")
            mesh_fineness = float(mesh_fineness)
            if not math.isfinite(mesh_fineness) or mesh_fineness <= 0.0:
                raise ValueError(
                    "motion_fea_handoff: mesh_fineness must be finite and > 0.")
            dyn_opts = coerce_json_or_repr({} if dynamics is None else dynamics)
            if not isinstance(dyn_opts, dict):
                raise ValueError("motion_fea_handoff: dynamics must be an object/dict.")

            mates = _asm_mates(d)
            if not mates:
                raise ValueError("motion_fea_handoff: document has no mates.")
            mate_by_id = {m.get("id"): m for m in mates if m.get("id")}
            drivable = [m for m in mates
                        if m.get("type") in _DRIVABLE_MATE_TYPES and not m.get("suppressed")]
            if mate_id is None:
                if len(drivable) != 1:
                    ids = ", ".join(str(m.get("id")) for m in drivable) or "none"
                    raise ValueError(
                        "motion_fea_handoff: mate_id may be omitted only when exactly one "
                        f"angle/distance mate is drivable (candidates: {ids}).")
                driven = drivable[0]
                mate_id = driven.get("id")
            else:
                driven = next((m for m in drivable if m.get("id") == mate_id), None)
                if driven is None:
                    raise ValueError(
                        f"motion_fea_handoff: mate '{mate_id}' is not a drivable "
                        "angle/distance mate.")

            def _mate_value(m: dict) -> float:
                value = m.get("value")
                if isinstance(value, dict):
                    value = value.get("value")
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError(
                        f"motion_fea_handoff: mate '{mate_id}' has no numeric value.")
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError(
                        f"motion_fea_handoff: mate '{mate_id}' value is non-finite.")
                return value

            if start is None:
                start = _mate_value(driven)
            start = float(start)
            if not math.isfinite(start):
                raise ValueError("motion_fea_handoff: start must be finite.")

            # --- resolve the isolated component and its two explicit load faces ----
            tr = cb.call("doc_tree", {"doc_id": d}).get("tree") or {}
            comps = [c for c in (tr.get("components") or []) if not c.get("suppressed")]
            comp = next((c for c in comps if c.get("id") == component or
                         (c.get("name") or "") == component), None)
            if comp is None:
                ids = ", ".join(f"{c.get('id')}({c.get('name')})" for c in comps)
                raise ValueError(f"motion_fea_handoff: component '{component}' not found. "
                                 f"Components: {ids}")
            cid = comp.get("id")
            if comp.get("fixed"):
                raise ValueError(
                    "motion_fea_handoff: the isolated component must be moving, not fixed.")
            src_path = (comp.get("source") or {}).get("path") or ""
            if not src_path or not os.path.exists(src_path):
                raise ValueError(f"motion_fea_handoff: component '{cid}' has no readable "
                                 f"source file ({src_path!r}) to run FEA on.")
            part_doc = cb.call("doc_open", {"path": src_path}).get("doc_id")
            if not part_doc:
                raise ValueError("motion_fea_handoff: component part could not be opened.")
            fixture_selector = _resolve_one_selector(part_doc, fixtures[0], kind="face")
            fixture_sets = _resolve_face_points(part_doc, fixture_selector)

            def _finite_vec(raw, label):
                if not isinstance(raw, list) or len(raw) != 3:
                    raise ValueError(f"motion_fea_handoff: {label} must have 3 components.")
                vec = [float(x) for x in raw]
                if not all(math.isfinite(x) for x in vec):
                    raise ValueError(f"motion_fea_handoff: {label} must be finite.")
                return vec

            def _centroid(selector):
                if not isinstance(selector, dict) or selector.get("kind") != "face":
                    return None
                raw = selector.get("centroid")
                if not isinstance(raw, list) or len(raw) != 3:
                    return None
                if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and
                           math.isfinite(float(x)) for x in raw):
                    return None
                return [float(x) for x in raw]

            def _same_face(left, right):
                left_sid, right_sid = left.get("sid"), right.get("sid")
                if left_sid and right_sid:
                    return left_sid == right_sid
                lc, rc = _centroid(left), _centroid(right)
                if lc is None or rc is None:
                    return False
                if math.sqrt(sum((a - b) ** 2 for a, b in zip(lc, rc))) > 1e-6:
                    return False
                ln, rn = left.get("normal"), right.get("normal")
                if (isinstance(ln, list) and len(ln) == 3 and
                        isinstance(rn, list) and len(rn) == 3):
                    ln, rn = _finite_vec(ln, "fixture face normal"), _finite_vec(
                        rn, "mate face normal")
                    lmag = math.sqrt(sum(x * x for x in ln))
                    rmag = math.sqrt(sum(x * x for x in rn))
                    if lmag <= 1e-12 or rmag <= 1e-12:
                        return False
                    if sum(a * b for a, b in zip(ln, rn)) / (lmag * rmag) < 1.0 - 1e-6:
                        return False
                return True

            if _centroid(fixture_selector) is None:
                raise ValueError(
                    "motion_fea_handoff: fixture query must resolve one stable face with "
                    "a finite centroid; tied/predicate-only fixture queries are ambiguous.")

            # --- motion study WITH dynamics (engine-side immutable sweep) ---------
            r_mot = cb.call("simulate_motion", {
                "doc_id": d, "mate_id": mate_id, "start": start,
                "stop": stop, "steps": steps,
                "check_interference": False, "min_clearance_mm": 0.0,
                "dynamics": dyn_opts})
            dyn = r_mot.get("dynamics") or {}
            if dyn and (not dyn.get("ok") or not dyn.get("force_path_resolved")):
                return _dumps({
                    "ok": False,
                    "error": "motion dynamics has an unresolved force/moment path; "
                             "refusing to create FEA loads",
                    "warnings": dyn.get("warnings")})
            if not r_mot.get("ok"):
                return _dumps({
                    "ok": False,
                    "error": r_mot.get("error", "motion sweep did not converge"),
                    "warnings": dyn.get("warnings")})
            frames = dyn.get("frames") or []
            if not frames or not all(fr.get("balanced") for fr in frames):
                return _dumps({
                    "ok": False,
                    "error": "motion dynamics produced no fully balanced frame set",
                    "warnings": dyn.get("warnings")})
            drv_mate = r_mot.get("mate_id")

            # Every solved joint on this body must have one stable component-side ref.
            component_refs = {}
            for m in mates:
                refs = [ref for ref in (m.get("refs") or [])
                        if isinstance(ref, dict) and ref.get("component") == cid]
                if refs:
                    component_refs[m.get("id")] = refs
            joint_meta = {j.get("mate_id"): j for j in (dyn.get("joints") or [])}
            touch_mids = {
                mid for mid, joint in joint_meta.items()
                if cid in (joint.get("component_a"), joint.get("component_b"))
            }
            if not touch_mids:
                return _dumps({
                    "ok": False,
                    "error": f"no solved joint reactions touch component '{cid}'",
                    "warnings": dyn.get("warnings")})
            bad_refs = [mid for mid in sorted(touch_mids)
                        if len(component_refs.get(mid) or []) != 1]
            if bad_refs:
                return _dumps({
                    "ok": False,
                    "error": "each solved joint on the isolated component must have exactly "
                             f"one component-side face reference; invalid mates: {bad_refs}",
                    "warnings": dyn.get("warnings")})
            selectors = {mid: component_refs[mid][0].get("selector") or {}
                         for mid in touch_mids}
            invalid_faces = [mid for mid, selector in selectors.items()
                             if _centroid(selector) is None]
            if invalid_faces:
                return _dumps({
                    "ok": False,
                    "error": "joint reaction faces are missing stable finite face selectors "
                             f"on component '{cid}': {sorted(invalid_faces)}",
                    "warnings": dyn.get("warnings")})

            fixed_ids = {c.get("id") for c in comps if c.get("fixed")}

            def _is_grounded_fixture(mid):
                mate = mate_by_id.get(mid) or {}
                other_ids = {
                    ref.get("component") for ref in (mate.get("refs") or [])
                    if isinstance(ref, dict) and ref.get("component") != cid
                }
                return (len(other_ids) == 1 and next(iter(other_ids)) in fixed_ids and
                        _same_face(selectors[mid], fixture_selector))

            fixture_candidates = sorted(mid for mid in touch_mids
                                        if _is_grounded_fixture(mid))
            setup_diagnostics = {
                "fixture_face": fixture_selector,
                "fixture_mate_candidates": fixture_candidates,
                "fixture_mate_id": fixture_mate_id,
                "fixture_reaction_omitted_from_loads": False,
                "body_load_face": None,
            }
            if len(fixture_candidates) != 1:
                return _dumps({
                    "ok": False,
                    "error": "fixture face must identify exactly one solved mate to a "
                             "fixed component; duplicate or missing grounded mates are an "
                             f"ambiguous boundary (candidates: {fixture_candidates})",
                    "setup_diagnostics": setup_diagnostics,
                    "warnings": dyn.get("warnings")})
            resolved_fixture_mate_id = fixture_candidates[0]
            if (fixture_mate_id is not None and
                    fixture_mate_id != resolved_fixture_mate_id):
                return _dumps({
                    "ok": False,
                    "error": f"fixture_mate_id '{fixture_mate_id}' is not the solved mate "
                             "joining the requested fixture face to a fixed component",
                    "setup_diagnostics": setup_diagnostics,
                    "warnings": dyn.get("warnings")})
            fixture_mate_id = resolved_fixture_mate_id
            setup_diagnostics["fixture_mate_id"] = fixture_mate_id
            setup_diagnostics["fixture_reaction_omitted_from_loads"] = True

            def _peak(fr: dict) -> float:
                peak = 0.0
                for reaction in fr.get("reactions") or []:
                    if reaction.get("mate_id") not in touch_mids:
                        continue
                    vec = _finite_vec(reaction.get("force_n"), "reaction force_n")
                    peak = max(peak, math.sqrt(sum(x * x for x in vec)))
                return peak

            if isinstance(at, str):
                if at != "worst":
                    raise ValueError("motion_fea_handoff: at must be 'worst' or a time in s.")
                frame = max(frames, key=_peak)
            else:
                t_want = float(at)
                if not math.isfinite(t_want):
                    raise ValueError("motion_fea_handoff: at time must be finite.")
                t_min = float(frames[0].get("t_s", 0.0))
                t_max = float(frames[-1].get("t_s", 0.0))
                if t_want < t_min or t_want > t_max:
                    raise ValueError(
                        f"motion_fea_handoff: at={t_want:g}s is outside "
                        f"[{t_min:g},{t_max:g}]s.")
                frame = min(frames, key=lambda fr: abs(float(fr.get("t_s", 0.0)) -
                                                        t_want))
            fi, ft = int(frame.get("i", 0)), float(frame.get("t_s", 0.0))
            if _peak(frame) <= 1e-12:
                return _dumps({
                    "ok": False,
                    "error": f"selected frame {fi} has zero force reactions on component "
                             f"'{cid}'; refusing a zero-load FEA handoff",
                    "setup_diagnostics": setup_diagnostics,
                    "warnings": dyn.get("warnings")})
            case = f"frame{fi}@t{ft:.6g}s"

            # Dynamics reactions are world-frame and act on component_b.  Rotate
            # them into the isolated part and form each joint wrench about its local
            # origin (mate selectors are local mm; moments use metres).
            trajectory_frame = next(
                (fr for fr in (r_mot.get("trajectory") or [])
                 if int(fr.get("i", -1)) == fi), None)
            pose = next(
                (b.get("transform") for b in ((trajectory_frame or {}).get("bodies") or [])
                 if b.get("component") == cid), None)
            if not isinstance(pose, list) or len(pose) != 16:
                raise ValueError(
                    "motion_fea_handoff: selected component pose is unavailable.")
            pose = [float(x) for x in pose]  # engine convention: column-major
            if not all(math.isfinite(x) for x in pose):
                raise ValueError(
                    "motion_fea_handoff: selected component pose is non-finite.")

            def _world_to_local(vec):
                return [sum(pose[c * 4 + r] * vec[r] for r in range(3))
                        for c in range(3)]

            def _vadd(left, right):
                return [a + b for a, b in zip(left, right)]

            def _vsub(left, right):
                return [a - b for a, b in zip(left, right)]

            def _vneg(vec):
                return [-x for x in vec]

            def _cross(left, right):
                return [left[1] * right[2] - left[2] * right[1],
                        left[2] * right[0] - left[0] * right[2],
                        left[0] * right[1] - left[1] * right[0]]

            def _norm(vec):
                return math.sqrt(sum(x * x for x in vec))

            reaction_rows = []
            for reaction in frame.get("reactions") or []:
                mid = reaction.get("mate_id")
                if mid not in touch_mids:
                    continue
                meta = joint_meta[mid]
                if cid == meta.get("component_b"):
                    sign = 1.0
                elif cid == meta.get("component_a"):
                    sign = -1.0
                else:
                    continue
                force_world = [sign * x for x in
                               _finite_vec(reaction.get("force_n"), "reaction force_n")]
                moment_world = [sign * x for x in
                                _finite_vec(reaction.get("moment_nm"), "reaction moment_nm")]
                force_local = _world_to_local(force_world)
                moment_local = _world_to_local(moment_world)
                centroid = _centroid(selectors[mid])
                arm_m = [x * 1e-3 for x in centroid]
                wrench_moment = _vadd(moment_local, _cross(arm_m, force_local))
                reaction_rows.append({
                    "mate_id": mid,
                    "selector": selectors[mid],
                    "centroid_mm": centroid,
                    "force_world_n": force_world,
                    "force_local_n": force_local,
                    "joint_moment_local_nm": moment_local,
                    "wrench_moment_about_origin_local_nm": wrench_moment,
                })
            fixture_reaction = next(
                (row for row in reaction_rows if row["mate_id"] == fixture_mate_id), None)
            if fixture_reaction is None:
                return _dumps({
                    "ok": False,
                    "error": f"selected frame has no reaction for fixture mate "
                             f"'{fixture_mate_id}'",
                    "setup_diagnostics": setup_diagnostics,
                    "warnings": dyn.get("warnings")})

            zero = [0.0, 0.0, 0.0]
            joint_force_sum = list(zero)
            joint_moment_sum = list(zero)
            for row in reaction_rows:
                joint_force_sum = _vadd(joint_force_sum, row["force_local_n"])
                joint_moment_sum = _vadd(
                    joint_moment_sum,
                    row["wrench_moment_about_origin_local_nm"])
            body_force = _vneg(joint_force_sum)
            body_moment = _vneg(joint_moment_sum)
            moment_scale = max(
                [1.0, _norm(body_moment)] +
                [_norm(row["wrench_moment_about_origin_local_nm"])
                 for row in reaction_rows])
            force_tol = 1e-10 + 1e-8 * max(1.0, _norm(joint_force_sum))
            moment_tol = 1e-9 + 1e-6 * moment_scale
            balance_diagnostics = {
                "source_frame_balanced": bool(frame.get("balanced")),
                "source_force_residual_n": frame.get("force_residual_n"),
                "source_moment_residual_nm": frame.get("moment_residual_nm"),
                "joint_force_sum_local_n": joint_force_sum,
                "joint_moment_sum_about_origin_local_nm": joint_moment_sum,
                "equivalent_body_inertial_force_local_n": body_force,
                "equivalent_body_inertial_moment_about_origin_local_nm": body_moment,
                "omitted_fixture_reaction": fixture_reaction,
            }

            # A non-fixture joint couple cannot be represented by the static solver's
            # face-force load.  Refuse rather than silently dropping it.
            unsupported_couples = [
                {"mate_id": row["mate_id"],
                 "moment_local_nm": row["joint_moment_local_nm"]}
                for row in reaction_rows
                if row["mate_id"] != fixture_mate_id and
                _norm(row["joint_moment_local_nm"]) > moment_tol
            ]
            if unsupported_couples:
                balance_diagnostics["unsupported_nonfixture_joint_couples"] = (
                    unsupported_couples)
                return _dumps({
                    "ok": False,
                    "error": "non-fixture joint reactions include pure moments that the "
                             "face-force static solver cannot apply",
                    "setup_diagnostics": setup_diagnostics,
                    "balance_diagnostics": balance_diagnostics,
                    "warnings": dyn.get("warnings")})

            conditions = [
                sh.condition(
                    f"{fixture_mate_id}_fixed_support", "fixed_support", 1.0, "1",
                    f"fixture face associated with grounded mate '{fixture_mate_id}'; "
                    "its motion reaction is intentionally omitted",
                    applied_to=f"{cid}:face@{fixture_reaction['centroid_mm']}", case=case)
            ]
            loads = []
            warnings = list(dyn.get("warnings") or [])
            represented_joint_force = list(zero)
            represented_joint_moment = list(zero)
            for row in reaction_rows:
                if row["mate_id"] == fixture_mate_id:
                    continue
                if _norm(row["force_local_n"]) <= force_tol:
                    continue
                mid = row["mate_id"]
                face = {"of": "face", "near": row["centroid_mm"]}
                provenance = (
                    f"simulate_motion dynamics frame {fi} (t={ft:.6g}s) joint '{mid}' "
                    f"reaction on component '{cid}', transformed world to component-local")
                conditions.append(sh.condition(
                    f"{mid}_reaction_force", "force", row["force_local_n"], "N",
                    provenance, applied_to=f"{cid}:face@{row['centroid_mm']}", case=case))
                loads.append({
                    "kind": "joint_reaction",
                    "source": f"mate '{mid}'",
                    "face": face,
                    "force": row["force_local_n"],
                    "units": "N",
                    "force_world_n": row["force_world_n"],
                    "mate_id": mid})
                represented_joint_force = _vadd(
                    represented_joint_force, row["force_local_n"])
                represented_joint_moment = _vadd(
                    represented_joint_moment,
                    _cross([x * 1e-3 for x in row["centroid_mm"]],
                           row["force_local_n"]))

            represented_body_moment = list(zero)
            if _norm(body_force) > force_tol:
                if body_load_face is None:
                    balance_diagnostics["body_load_representation_complete"] = False
                    return _dumps({
                        "ok": False,
                        "error": "body_load_face is required to apply the non-zero equivalent "
                                 "gravity/inertial/body-load resultant",
                        "required_body_load": {
                            "force_local_n": body_force,
                            "moment_about_origin_local_nm": body_moment},
                        "setup_diagnostics": setup_diagnostics,
                        "balance_diagnostics": balance_diagnostics,
                        "warnings": warnings})
                body_selector = _resolve_one_selector(part_doc, body_load_face, kind="face")
                if _same_face(body_selector, fixture_selector):
                    return _dumps({
                        "ok": False,
                        "error": "body_load_face must be distinct from the fixed fixture face; "
                                 "a load on constrained nodes would not stress the component",
                        "setup_diagnostics": setup_diagnostics,
                        "balance_diagnostics": balance_diagnostics,
                        "warnings": warnings})
                body_centroid = _centroid(body_selector)
                if body_centroid is None:
                    raise ValueError(
                        "motion_fea_handoff: body_load_face lacks a finite face centroid.")
                represented_body_moment = _cross(
                    [x * 1e-3 for x in body_centroid], body_force)
                body_moment_error = _vsub(represented_body_moment, body_moment)
                if _norm(body_moment_error) > moment_tol:
                    balance_diagnostics.update({
                        "body_load_representation_complete": False,
                        "body_load_face_moment_local_nm": represented_body_moment,
                        "body_load_moment_error_nm": _norm(body_moment_error),
                        "body_load_moment_tolerance_nm": moment_tol,
                    })
                    return _dumps({
                        "ok": False,
                        "error": "body_load_face line of action does not reproduce the "
                                 "required equivalent body/inertial moment; choose a face on "
                                 "the resultant line of action",
                        "setup_diagnostics": setup_diagnostics,
                        "balance_diagnostics": balance_diagnostics,
                        "warnings": warnings})
                setup_diagnostics["body_load_face"] = body_selector
                provenance = (
                    f"negative sum of all joint wrenches on component '{cid}' at motion "
                    f"frame {fi} (t={ft:.6g}s), representing gravity, inertia, damping "
                    "and explicit dynamics loads")
                conditions.append(sh.condition(
                    "equivalent_body_inertial_force", "equivalent_body_inertial_force",
                    body_force, "N", provenance,
                    applied_to=f"{cid}:face@{body_centroid}", case=case))
                loads.append({
                    "kind": "equivalent_body_inertial",
                    "source": "inverse-dynamics joint-wrench balance",
                    "face": body_selector,
                    "force": body_force,
                    "units": "N",
                    "equivalent_moment_about_origin_nm": body_moment})
                warnings.append(
                    "equivalent gravity/inertial/body resultant is applied as a "
                    "statically equivalent face force, not a distributed body load")
            elif _norm(body_moment) > moment_tol:
                balance_diagnostics["body_load_representation_complete"] = False
                return _dumps({
                    "ok": False,
                    "error": "inverse dynamics requires a pure equivalent body/inertial "
                             "couple, which the face-force static solver cannot apply",
                    "setup_diagnostics": setup_diagnostics,
                    "balance_diagnostics": balance_diagnostics,
                    "warnings": warnings})

            if not loads:
                return _dumps({
                    "ok": False,
                    "error": "isolated snapshot has no non-zero representable load after "
                             "omitting the fixture reaction",
                    "setup_diagnostics": setup_diagnostics,
                    "balance_diagnostics": balance_diagnostics,
                    "warnings": warnings})

            applied_force = _vadd(represented_joint_force, body_force)
            applied_moment = _vadd(represented_joint_moment, represented_body_moment)
            force_closure = _vadd(
                applied_force, fixture_reaction["force_local_n"])
            moment_closure = _vadd(
                applied_moment,
                fixture_reaction["wrench_moment_about_origin_local_nm"])
            balance_diagnostics.update({
                "body_load_representation_complete": True,
                "body_load_face_moment_local_nm": represented_body_moment,
                "applied_nonfixture_force_sum_local_n": applied_force,
                "applied_nonfixture_moment_sum_about_origin_local_nm": applied_moment,
                "force_closure_residual_n": _norm(force_closure),
                "moment_closure_residual_nm": _norm(moment_closure),
                "expected_fixture_boundary_reaction_force_local_n":
                    fixture_reaction["force_local_n"],
                "expected_fixture_boundary_reaction_moment_about_origin_local_nm":
                    fixture_reaction["wrench_moment_about_origin_local_nm"],
            })
            if (_norm(force_closure) > force_tol or
                    _norm(moment_closure) > moment_tol):
                return _dumps({
                    "ok": False,
                    "error": "isolated static load set does not close against the omitted "
                             "fixture reaction",
                    "setup_diagnostics": setup_diagnostics,
                    "balance_diagnostics": balance_diagnostics,
                    "warnings": warnings})

            load_specs = []
            for load in loads:
                faces = _merge_face_sets(_resolve_face_points(part_doc, load["face"]))
                if not faces:
                    return _dumps({
                        "ok": False,
                        "error": f"{load['source']} face did not resolve on the component part",
                        "setup_diagnostics": setup_diagnostics,
                        "balance_diagnostics": balance_diagnostics,
                        "warnings": warnings})
                load_specs.append({"faces": faces, "type": "force",
                                   "vector": load["force"]})

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
                      "case": case,
                      "fixture_mate_id": fixture_mate_id,
                      "material": material if isinstance(material, str) else "custom"})

            result = {
                "ok": True,
                "doc_id": d,
                "component": cid,
                "part_doc_id": part_doc,
                "frame": {"i": fi, "t_s": ft},
                "loads_applied": loads,
                "setup_diagnostics": setup_diagnostics,
                "balance_diagnostics": balance_diagnostics,
                "warnings": warnings,
                "fea": None,
            }
            if dry_run:
                result["note"] = "dry_run: balanced loads + envelope built, FEA solve skipped"
                return _dumps(sh.attach(result, env))

            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": part_doc, "path": step})
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "loads": load_specs, "mesh_order": 2, "fineness": mesh_fineness}
            _autosave_all_open()
            res = cb.call("sim_run_static", {"study": study})
            if not res.get("ok"):
                result["ok"] = False
                result["fea"] = {"ok": False, "error": res.get("error", "solve failed")}
                return _dumps(sh.attach(result, env))
            _persist_study(part_doc, {
                "type": "fea_static",
                "name": f"Motion isolated snapshot {case} ({mat.get('name', material)})",
                "engine": "cpp", "material": mat, "fixtures": fixtures,
                "fixture_mate_id": fixture_mate_id,
                "loads": [{"kind": ld["kind"], "face": ld["face"],
                           "force": ld["force"], "units": "N"}
                          for ld in loads],
                "mesh_fineness": mesh_fineness,
                "results": res.get("summary", {}), "n_dof": res.get("n_dof"),
                "simhandoff_id": env.get("id"),
            })
            result["fea"] = {"ok": True, "summary": res.get("summary", {}),
                             "n_dof": res.get("n_dof"),
                             "material": mat.get("name", material)}
            return _dumps(sh.attach(result, env))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def list_world_bodies(explode: bool = False, doc_id: Optional[str] = None) -> str:
        """List a doc's bodies with their WORLD-placed bounding boxes.

        Works on an ASSEMBLY doc (each placed component after mates are solved) AND on a
        multi-body PART — e.g. one assembled via import_part + transform (where the bodies are
        already placed absolutely). Returns each body {name, component, bbox:{min,max,size},
        placement_source}; use it to see where every part sits in the shared frame, or pair
        with check_interference to validate fit. explode=True applies the exploded-view offsets
        on an assembly (ignored on a plain multi-body part).

        `placement_source` is "mate" (solver-derived pose), "transform" (a pose YOU asserted
        via asm_add_component at/rotate — verify it with check_interference + measure_between),
        "origin" (inserted at identity) or null (this process did not place it). Any
        transform-placed component still carries `placement_warnings` here and on
        save_part/export_part until check_interference runs again.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("asm_world_bodies", {"doc_id": d, "explode": bool(explode)})
            bodies = r.get("bodies", [])
            for b in bodies:
                if isinstance(b, dict):
                    b["placement_source"] = placement_source_of(d, b.get("component"))
            warnings = placement_warnings(d)
            out = {"ok": True, "doc_id": d, "body_count": len(bodies), "bodies": bodies}
            if r.get("multibody_part"):
                out["multibody_part"] = True
            if warnings:
                out["placement_warnings"] = warnings
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)
