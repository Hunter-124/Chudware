"""CAD assembly + motion MCP tools (split out of cad_tools.py): new_assembly, asm_add_component, asm_mate, solve_mates, simulate_motion, motion_fea_handoff, list_world_bodies."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
from contextlib import contextmanager
import threading
import os
import copy
import hashlib
import struct
import math
import json
from types import SimpleNamespace

from .cad_common import (
    _CUR,
    _autosave,
    _resolve_material_density,
    _describe_type,
    _doc,
    _doc_new_scope,
    _docs_named,
    _docs_in_version_series,
    _dumps,
    _err,
    _QVOCAB_HELP,
    _SID_RE,
    _as_face_query,
    _is_empty_slot_word,
    _merge_face_sets,
    _normalize_optional_slot,
    _persist_study,
    _pick_best_doc,
    _publish_current,
    _resolve_face_points,
    _resolve_one_selector,
    _resolve_material,
    _resumable_docs,
    _sid_match,
    _strip_version_suffix,
    _tmp,
    _version_series_base,
    cb,
    coerce_json_or_repr,
    claim_process_doc,
    note_placement_source,
    placement_source_of,
    placement_warnings,
    pose_to_transform16,
    require_list,
    resolve_agent_path,
    sh,
)


def _solve_report(rebuild: dict, include_mate: Optional[str] = None) -> dict:
    """Read the mate solve's verdict out of a doc_apply_ops/doc_solve_mates rebuild.

    Returns the worst five mates and, when requested, the newly added mate, with
    total and omitted counts. `message` is
    empty when the solve converged; otherwise it NAMES the mate holding it back and by
    how much, which is the one thing a caller needs to fix a diverging network — the
    engine used to report `doc_status.mates == "ok"` for a solve that never converged.
    """
    rb = rebuild if isinstance(rebuild, dict) else {}
    mates = [m for m in (rb.get("mates") or []) if isinstance(m, dict)]
    ranked = sorted(mates, key=lambda m: -float(m.get("residual") or 0.0))[:5]
    selected = next((m for m in mates if m.get("id") == include_mate), None)
    if selected is not None and not any(m.get("id") == include_mate for m in ranked):
        ranked.append(selected)
    worst = [{"id": m.get("id"), "type": m.get("type"), "status": m.get("status"),
              "residual": float(m.get("residual") or 0.0), "reason": m.get("reason")}
             for m in ranked]
    # The engine states its verdict in `converged`. A rebuild without it (a failed apply, a
    # non-assembly document) is NOT a converged solve — defaulting to True once reported an
    # unsolved network as solved.
    verdict = rb.get("converged")
    verdict_missing = not isinstance(verdict, bool)
    converged = verdict is True
    worst_mate = rb.get("worst_mate") or (worst[0]["id"] if worst else None)
    worst_res = float(rb.get("max_residual") or 0.0)
    over = bool(rb.get("over_defined", False))
    msg = ""
    unresolved = next((m for m in mates if m.get("status") == "unresolved"), None)
    reason = ("mate_unresolved" if unresolved
              else "solve_verdict_missing" if verdict_missing else None)
    if unresolved:
        converged = False
        worst_mate = unresolved.get("id")
    if not converged:
        if verdict_missing:
            msg = ("the engine returned no mate-solve verdict (`converged` is missing from the "
                   "rebuild), so this solve is not treated as converged. Re-run solve_mates; "
                   "if it recurs the document is not an assembly or its rebuild failed.")
        else:
            msg = (f"mate solve did not converge: worst mate '{worst_mate}' "
                   f"residual {worst_res:.6g} (tolerance {float(rb.get('tolerance') or 0.0):.3g}; "
                   "units are mm for positional rows, dimensionless for direction rows, "
                   "and radians for angle rows). "
                   "Components stay near their authored poses — the solver no longer moves "
                   "an assembly it cannot solve. Fix or suppress that mate (check its align "
                   "sense and that both refs are the intended faces/axes), then re-solve.")
            if over:
                msg += " The network is also OVER-DEFINED; inspect the conflicting constraints."
        if unresolved:
            msg = (f"mate '{worst_mate}' is unresolved: "
                   f"{unresolved.get('reason') or 'mate references could not be resolved'}")
    blockers = [{"id": m.get("id"), "type": m.get("type"), "status": m.get("status"),
                 "residual": float(m.get("residual") or 0.0), "reason": m.get("reason")}
                for m in mates if m.get("status") in ("unresolved", "conflicting", "unsatisfied")]
    # A component whose source failed to build is not placed; mates on it are unresolved.
    source_problems = [{"component": c.get("id"), "error": c.get("error")}
                       for c in (rb.get("components") or [])
                       if isinstance(c, dict) and c.get("ok") is False]
    return {"converged": converged, "worst_mate": worst_mate,
            "worst_residual": worst_res, "tolerance": rb.get("tolerance"),
            "mates_over_defined": over, "worst": worst, "mate_total": len(mates),
            "mate_omitted": len(mates) - len(worst), "message": msg, "reason": reason,
            "blocking_mates": blockers, "source_problems": source_problems}

def component_source_base(doc_id: Optional[str]) -> Optional[str]:
    """The folder the engine resolves relative component sources against.

    It is the assembly file's folder, else the project folder that doc_new received
    (core/doc/session.cpp component_source_base). None when neither exists. The engine is
    the one authority: the worker cwd is never a base."""
    if not doc_id:
        return None
    base = (cb.call("doc_tree", {"doc_id": doc_id}) or {}).get("component_source_base")
    return base if isinstance(base, str) and base else None


def resolve_component_source(path: str, base: Optional[str]) -> Optional[str]:
    """Absolute path of a stored component `source.path`, resolved as compile_assembly does.

    None for a relative path when `base` is None."""
    p = str(path)
    if os.path.isabs(p):
        return os.path.normpath(p)
    if not base:
        return None
    return os.path.normpath(os.path.join(base, p))


# Read-only source snapshots for mate queries, keyed by absolute path and file stamp.
# Keep a lease through selector resolution so a concurrent refresh cannot close a reader.
_ASM_QUERY_DOCS: dict = {}
_ASM_QUERY_LOCK = threading.RLock()


def _component_source_path(path: str, asm_doc: Optional[str]) -> str:
    """Absolute path of a component source STORED in the assembly document.

    A relative stored source is relative to the ASSEMBLY file's folder (`Parts/…`,
    `../Frame/…`), else to the project folder of an unsaved assembly. The engine
    reports that base (component_source_base), so this never disagrees with
    compile_assembly. It was resolved against the worker's cwd here, so asm_mate on a
    saved assembly failed with "no such file" for every relative component (2026-09-24)."""
    resolved = resolve_component_source(path, component_source_base(asm_doc))
    if resolved is None:
        raise ValueError(
            f"component source {path!r} is a relative path. The assembly has no file "
            "and no project folder to resolve it. Give an absolute path, or save the "
            "assembly first.")
    return resolved


def component_source_file(comp: dict, asm_doc: Optional[str]) -> str:
    """Absolute file the engine opens for a component's stored source (the one resolver).

    Raises ValueError for an embedded component (no file) or an unresolvable relative path."""
    path = ((comp.get("source") or {}).get("path")) or ""
    if not path:
        raise ValueError(f"component '{comp.get('id')}' has no source part file; its geometry "
                         "cannot be read from disk")
    return _component_source_path(path, asm_doc)


def _file_stamp(path: str) -> tuple:
    """Identity of a file's current bytes: device, inode, size and both change times."""
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _open_docs_by_path() -> dict:
    """{real path: doc_list row} for every open document bound to a file.

    The assembly compiles from the LIVE open document of a component's file when one exists
    (core/doc/assembly.cpp current_assembly_source), so these are the sources it really uses.
    Private internal copies are detached from their path and never appear here."""
    out: dict = {}
    for row in (cb.call("doc_list", {}) or {}).get("docs", []):
        if isinstance(row, dict) and isinstance(row.get("path"), str) and row["path"]:
            out[os.path.realpath(row["path"])] = row
    return out


@contextmanager
def open_component_part(path: str):
    """A read handle on the document a component's source file is built from; never a GUI tab.

    The assembly compiles from the LIVE open document of that file when one exists, so that
    document is used as it is (read-only here, never closed). Otherwise a private copy is
    opened `internal` (invisible to every client) and `detach_path` (no claim on the part's
    save path) and closed on exit — a plain doc_open fans `doc.opened` out to the GUI, which
    opens a tab for the user, and binds a second session to the part's save path."""
    live = _open_docs_by_path().get(os.path.realpath(path))
    if live is not None:
        yield live["doc_id"]
        return
    opened = cb.call("doc_open", {"path": path, "internal": True, "detach_path": True})
    did = opened.get("doc_id") if isinstance(opened, dict) else None
    if not did:
        raise ValueError(f"component source {path!r} could not be opened")
    try:
        yield did
    finally:
        try:
            cb.call("doc_close", {"doc_id": did})
        except Exception:  # noqa: BLE001 — a private copy we cannot close is not a result
            pass


def source_states(asm_doc: str, comps: list, status: Optional[dict] = None) -> list:
    """Report stale fingerprints independently of open/unsaved and missing sources.

    Pass a status captured before a solve or geometry read to preserve the pre-refresh state.
    """
    status = status if status is not None else cb.call("asm_source_status", {"doc_id": asm_doc})
    out: list = []
    source_rows = status.get("sources") or []
    for row in source_rows:
        common = {"component": row.get("component"), "source": row.get("path")}
        if row.get("changed") is True:
            out.append({**common, "state": "stale", "current": row.get("current"),
                        "cached": row.get("cached")})
        if row.get("open") and row.get("dirty"):
            out.append({**common, "state": "live_unsaved", "doc_id": row.get("doc_id")})
    base = None
    for comp in comps:
        if not isinstance(comp, dict):
            continue
        src = (comp.get("source") or {}).get("path")
        if not src:
            continue
        if base is None:
            base = component_source_base(asm_doc) or ""
        path = resolve_component_source(src, base or None)
        if path is None:
            out.append({"component": comp.get("id"), "source": src,
                        "state": "unresolvable_path"})
            continue
        source_row = next((r for r in source_rows if r.get("component") == comp.get("id")), {})
        if not source_row.get("open") and not os.path.exists(path):
            out.append({"component": comp.get("id"), "source": path,
                        "state": "missing_file"})
    return out


@contextmanager
def _asm_component_doc(path: str, asm_doc: Optional[str] = None):
    """Lease a read-only document matching the geometry the assembly is built from.

    The assembly compiles from the LIVE open document of a component's file, so when that part
    is open — saved or not — mate operands resolve against it: an unsaved edit is never
    resolved against its stale saved file, and nobody is asked to save. Otherwise a private
    snapshot of the saved file is leased, keyed on the file's stamp."""
    p = _component_source_path(path, asm_doc)
    live = _open_docs_by_path().get(os.path.realpath(p))
    if live is not None:
        yield live["doc_id"]
        return
    with _ASM_QUERY_LOCK:
        stamp = _file_stamp(p)
        cached = _ASM_QUERY_DOCS.get(p)
        if cached:
            d, saved_stamp = cached
            try:
                tree = cb.call("doc_tree", {"doc_id": d})
            except cb.CoreError as exc:
                if exc.code != "unknown_doc":
                    raise
                _ASM_QUERY_DOCS.pop(p, None)
            else:
                # A child assembly can change through its dependencies without its own
                # file changing. Leave recursive invalidation to the engine on reopen.
                if stamp == saved_stamp and (tree.get("tree") or {}).get("schema") == "aicad.part/1":
                    yield d
                    return
                cb.call("doc_close", {"doc_id": d})
                _ASM_QUERY_DOCS.pop(p, None)
        # A cached read-only query must not reserve the source's save destination.
        opened = cb.call("doc_open", {"path": p, "internal": True, "detach_path": True})
        d = opened["doc_id"]
        _ASM_QUERY_DOCS[p] = (d, stamp)
        yield d


def _component_sid_match(part_doc: str, asm_doc: str, comp: dict,
                          sid: str) -> Optional[dict]:
    """The component-part match a stable sid names, or None.

    The sid is looked up with cad_common's _sid_match (the lookup query_geometry/hover
    use) in the component's SOURCE part first. stable_sid hashes the
    WORLD centroid (core/doc/selectors.cpp), so a sid read off this assembly — its
    render tiles, query_geometry on the assembly — names the POSED instance instead:
    that match is mapped back through the component's transform onto the one part face/
    edge of the same kind+surf at that local centroid. A sid on a different component
    maps to nothing there and stays a miss."""
    m = _sid_match(part_doc, sid)
    if m is not None:
        return m
    world = _sid_match(asm_doc, sid)
    if world is None or world.get("centroid") is None:
        return None
    import numpy as np
    try:
        inverse = np.linalg.inv(np.asarray(comp.get("transform"), dtype=float)
                                .reshape(4, 4))
    except (TypeError, ValueError, np.linalg.LinAlgError):
        return None
    local = inverse[:3, :3] @ np.asarray(world["centroid"], dtype=float) + inverse[:3, 3]
    kind = world.get("kind") or "face"
    rows = (cb.call("doc_query", {"doc_id": part_doc, "query": {"of": kind}})
            .get("matches") or [])
    hits = [r for r in rows if isinstance(r, dict) and r.get("surf") == world.get("surf")
            and r.get("centroid") is not None
            and float(np.linalg.norm(np.asarray(r["centroid"], dtype=float) - local)) < 1e-3]
    return hits[0] if len(hits) == 1 else None


def _mate_signature(value) -> str:
    """Hash typed JSON using the same streaming representation as assembly.cpp."""
    digest = hashlib.sha256()

    def integer(number):
        digest.update((number & ((1 << 64) - 1)).to_bytes(8, "big"))

    def typed(item):
        if item is None:
            digest.update(b"n")
        elif isinstance(item, bool):
            digest.update(b"t" if item else b"f")
        elif isinstance(item, str):
            encoded = item.encode("utf-8")
            digest.update(b"s")
            integer(len(encoded))
            digest.update(encoded)
        elif isinstance(item, int):
            digest.update(b"i")
            integer(item)
        elif isinstance(item, float):
            digest.update(b"d")
            digest.update(struct.pack(">d", item))
        elif isinstance(item, list):
            digest.update(b"a")
            integer(len(item))
            for child in item:
                typed(child)
        elif isinstance(item, dict):
            digest.update(b"o")
            integer(len(item))
            for key in sorted(item):
                typed(key)
                typed(item[key])
        else:
            raise TypeError("unsupported source document value")

    typed(value)
    return digest.hexdigest()


def _mate_selector(part_doc: str, match: dict) -> dict:
    """Use the engine's pick, which carries the source feature owner and signed normal."""
    # An assembly pick is local to its child component, not to the source
    # assembly as a whole. Preserve the source-assembly query frame instead.
    tree = cb.call("doc_tree", {"doc_id": part_doc}).get("tree") or {}
    if tree.get("schema") == "aicad.asm/1":
        return match
    body, topo, kind = match.get("body"), match.get("topo"), match.get("kind")
    if not isinstance(body, int) or not isinstance(topo, int) or kind not in ("face", "edge"):
        return match
    pick = cb.call("doc_resolve_pick", {"doc_id": part_doc, "body_id": body,
                                        "topo_id": topo, "kind": kind})
    selector = pick.get("selector")
    if not isinstance(selector, dict):
        return match
    # Nested assembly picks report the owner beside their component-local selector.
    if isinstance(pick.get("owner_feature"), str):
        selector["owner_feature"] = pick["owner_feature"]
    if tree.get("schema") == "aicad.part/1" and isinstance(tree.get("features"), list):
        context = {key: value for key, value in tree.items() if key not in ("id", "features")}
        selector["_mate_source_state"] = {
            "context": _mate_signature(context),
            "features": [_mate_signature(feature) for feature in tree["features"]],
        }
    return selector


def _mate_branch(mate: dict, tree: dict, converged: bool) -> dict:
    """Describe the actual signed branch using component-local picks and solved poses."""
    import numpy as np
    components = {c.get("id"): c for c in (tree.get("components") or [])
                  if isinstance(c, dict)}
    rows = []
    for ref in mate.get("refs", []):
        sel = ref.get("selector") or {}
        comp = components.get(ref.get("component")) or {}
        try:
            pose = np.asarray(comp.get("transform"), dtype=float).reshape(4, 4)
            point = sel.get("axis_point") if sel.get("surf") in ("cylinder", "cone") else None
            point = np.asarray(point if point is not None else sel["centroid"], dtype=float)
            direction = sel.get("normal") if sel.get("surf") == "plane" else sel.get("axis")
            direction = np.asarray(direction, dtype=float)
            if (point.shape != (3,) or direction.shape != (3,) or
                    not np.isfinite(pose).all() or not np.isfinite(point).all() or
                    not np.isfinite(direction).all() or np.linalg.norm(direction) < 1e-12):
                return {"available": False}
            direction = pose[:3, :3] @ (direction / np.linalg.norm(direction))
            rows.append({"component": ref.get("component"), "surface": sel.get("surf"),
                         "point_world_mm": (pose[:3, :3] @ point + pose[:3, 3]).tolist(),
                         "direction_world": direction.tolist()})
        except (KeyError, TypeError, ValueError):
            return {"available": False}
    if len(rows) != 2:
        return {"available": False}
    result = {"available": True, "configuration": "solved" if converged else "provisional",
              "align": mate.get("align", "aligned"), "references": rows}
    if mate.get("type") == "coincident" and all(r["surface"] == "plane" for r in rows):
        result["orientation_rule"] = "opposite" if mate.get("align") != "anti" else "same"
    elif mate.get("type") in ("parallel", "concentric", "coincident"):
        result["orientation_rule"] = "same" if mate.get("align") != "anti" else "opposite"
    elif mate.get("type") == "distance":
        plane = 0 if rows[0]["surface"] == "plane" else 1 if rows[1]["surface"] == "plane" else None
        if plane is not None:
            normal = np.asarray(rows[plane]["direction_world"])
            gap = np.asarray(rows[1]["point_world_mm"]) - np.asarray(rows[0]["point_world_mm"])
            value = mate.get("value", {}).get("value")
            result.update({"normal_reference": "a" if plane == 0 else "b",
                           "reference_normal_world": normal.tolist(),
                           "signed_gap_mm": float(normal @ gap),
                           "target_signed_gap_mm": (-1 if mate.get("align") == "anti" else 1)
                                                   * value if isinstance(value, (int, float)) else None,
                           "equation": "normal dot (point_b - point_a) = sign * value"})
    return result


def _body_counts(bodies: list) -> dict:
    """World-body entries are logical members; source ordinals enumerate solids."""
    count = sum(len(b["source_ordinals"]) for b in bodies
                if isinstance(b, dict) and isinstance(b.get("source_ordinals"), list))
    complete = all(isinstance(b, dict) and isinstance(b.get("source_ordinals"), list)
                   for b in bodies)
    return {"logical_member_count": len(bodies), "physical_solid_count": count if complete else None}


def resolve_mate_ref(asm_doc: str, comps: dict, tag: str, ref: dict) -> dict:
    """Resolve one mate operand {component, query|sid} to {component, selector}.

    The query or sid resolves against the component's SOURCE part and must name
    exactly ONE face/edge. Returns {"ref": {component, selector}} or {"error": text}
    (plus "candidates" when the query matched several). asm_mate and add_features'
    asm_add_mate / asm_edit_mate share this resolver."""
    kid = ref.get("component")
    if kid not in comps:
        return {"error": f"{tag}: no component '{kid}' (have: {sorted(comps)})"}
    src = ((comps[kid].get("source") or {}).get("path")) or ""
    if not src:
        return {"error": f"{tag}: component '{kid}' has no source path "
                         "(embedded components not supported yet)"}
    q = ref.get("query", ref.get("sid", "top"))
    with _asm_component_doc(src, asm_doc) as part_doc:
        sid = None
        if isinstance(q, str) and _SID_RE.fullmatch(q.strip()):
            sid = q.strip()
        elif (isinstance(q, dict) and isinstance(q.get("sid"), str)
              and _SID_RE.fullmatch(q["sid"].strip())):
            sid = q["sid"].strip()
        if sid is not None:
            # A sid IS one element, not a query predicate — picks.cpp has no `sid`
            # predicate, and a bare string here used to reach the engine as
            # `side` ("geom query: bad 'side' 'F2bb10f'", live c245).
            match = _component_sid_match(part_doc, asm_doc, comps[kid], sid)
            if match is None:
                return {"error": f"{tag}: no face/edge of component '{kid}' carries "
                                 f"sid {sid!r} — sids move whenever geometry does. "
                                 f"Re-read them with describe_part on its source part "
                                 f"({src}) or query_geometry on this assembly, or pass a "
                                 f"geometry query {_QVOCAB_HELP}."}
            return {"ref": {"component": kid, "selector": _mate_selector(part_doc, match)}}
        if isinstance(q, str):
            try:
                q = _as_face_query(q)
            except ValueError as e:
                return {"error": f"{tag}: {e} A face/edge sid "
                                 "(\"F2bb10f\") is also accepted as the query."}
        matches = (cb.call("doc_query", {"doc_id": part_doc, "query": q})
                   .get("matches") or [])
        if len(matches) != 1:
            brief = [{k: m.get(k) for k in ("sid", "kind", "surf", "side",
                                            "centroid", "radius")}
                     for m in matches[:8]]
            return {"error": f"{tag}: query must resolve to exactly ONE face/edge of "
                             f"'{kid}' — got {len(matches)}. Narrow with pick/near/"
                             "count, or pass one candidate's sid as the query "
                             "(candidates are in the source-component local frame).",
                    "candidate_frame": "source_component_local", "candidates": brief}
        return {"ref": {"component": kid, "selector": _mate_selector(part_doc, matches[0])}}


# ------------------------------------------------------ asm_add_component pose arguments

def _finite_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _component_pose(at, rotate, angle) -> Optional[list]:
    """The row-major 4x4 for asm_add_component's at / rotate / angle; None when none was given.

    Refuses what used to become the wrong matrix and report ok: an `angle` with no `rotate`
    axis (silently ignored), a `rotate` axis with a zero angle (an identity), and a
    zero-length axis (a SCALING matrix, diag(cos, cos, cos))."""
    at = coerce_json_or_repr(at) if at is not None else None
    rotate = coerce_json_or_repr(rotate) if rotate is not None else None
    if not _finite_number(angle):
        raise ValueError(
            f"asm_add_component: angle must be a finite number of degrees, got {angle!r}.")
    if at is not None and (not isinstance(at, (list, tuple)) or len(at) != 3
                           or not all(_finite_number(v) for v in at)):
        raise ValueError("asm_add_component: `at` must be [x, y, z] — three finite numbers in mm.")
    if rotate is None:
        if angle != 0.0:
            raise ValueError(
                f"asm_add_component: angle={angle:g} comes without a rotation axis and would be "
                "ignored. Pass rotate=[ax, ay, az] with it, or drop the angle.")
        return None if at is None else pose_to_transform16(at, None, 0.0)
    if (not isinstance(rotate, (list, tuple)) or len(rotate) != 3
            or not all(_finite_number(v) for v in rotate)):
        raise ValueError("asm_add_component: `rotate` must be an axis [ax, ay, az] — three "
                         "finite numbers; the degrees go in `angle`.")
    if math.sqrt(sum(float(v) ** 2 for v in rotate)) <= 1e-12:
        raise ValueError("asm_add_component: `rotate` is a zero-length axis; give a direction "
                         "such as [0, 0, 1].")
    if angle == 0.0:
        raise ValueError("asm_add_component: `rotate` needs a nonzero `angle` in degrees — with "
                         "angle 0 nothing rotates. Drop `rotate` to insert unrotated.")
    return pose_to_transform16(at, rotate, angle)


# ------------------------------------------------------------------- asm_mate request

#: The mate types the engine builds (core/doc/validate.cpp MATE_TYPES, core/mate/mate.cpp
#: make()). The engine is the authority: asm_add_mate refuses any other type and lists these.
_MATE_TYPES = (
    "coincident", "parallel", "perpendicular", "tangent", "concentric", "distance", "angle",
    "lock", "width", "symmetric", "distance_limit", "angle_limit", "profile_center", "gear",
    "rack_pinion", "screw", "hinge", "slot", "cam_follower")
#: mate type -> (fewest, most) operands; the default is exactly two (validate.cpp
#: mate_ref_counts).
_MATE_OPERANDS = {"width": (3, 4), "symmetric": (3, 3), "hinge": (2, 4), "angle": (2, 3)}
_MATE_DIMENSION_TYPES = ("distance", "angle")
#: mate type -> (required numeric value keys, optional numeric value keys) (validate.cpp
#: mate_coupling_spec).
_MATE_VALUE_KEYS = {
    "distance_limit": (("min", "max"), ("value",)),
    "angle_limit": (("min", "max"), ("value",)),
    "gear": (("ratio",), ()),
    "screw": (("pitch",), ()),
    "rack_pinion": ((), ("pitch_radius",)),
    "hinge": ((), ("min", "max")),
}
_MATE_VALUE_EXAMPLES = {
    "distance_limit": {"min": 0, "max": 20}, "angle_limit": {"min": 0, "max": 90},
    "gear": {"ratio": 2}, "screw": {"pitch": 1.5}, "rack_pinion": {"pitch_radius": 12},
    "hinge": {"min": 0, "max": 120},
}


def _check_mate_request(mate_type, value, operands: int) -> str:
    """Normalize and validate asm_mate's type, value and operand count; returns the type.

    Mirrors the engine's own checks (validate.cpp validate_assembly, session.cpp asm_add_mate)
    so a bad request fails here — before any operand is resolved against a source part — with
    the vocabulary and the required parameters."""
    kind = str(mate_type or "").strip().lower()
    if kind not in _MATE_TYPES:
        raise ValueError(
            f"asm_mate: unknown mate_type {mate_type!r}. Valid types: {', '.join(_MATE_TYPES)}. "
            "asm_mate's description gives each type's operands and value.")
    lo, hi = _MATE_OPERANDS.get(kind, (2, 2))
    if not lo <= operands <= hi:
        want = str(lo) if lo == hi else f"{lo}..{hi}"
        raise ValueError(
            f"asm_mate: {kind} takes {want} operands but got {operands}. `a` and `b` are the "
            "first two; further operands go in extra=[...].")
    required, optional = _MATE_VALUE_KEYS.get(kind, ((), ()))
    if kind in _MATE_DIMENSION_TYPES:
        number = value.get("value") if isinstance(value, dict) else value
        if not _finite_number(number):
            unit = "degrees" if kind == "angle" else "mm"
            raise ValueError(f"asm_mate: {kind} needs `value` — a number of {unit}, or an object "
                             "with a numeric 'value'.")
    elif required or optional:
        example = json.dumps(_MATE_VALUE_EXAMPLES[kind])
        if value is None:
            if required:
                raise ValueError(f"asm_mate: {kind} needs value={example} "
                                 f"(required: {', '.join(required)}).")
        elif not isinstance(value, dict):
            raise ValueError(f"asm_mate: {kind} takes `value` as an object like {example}, "
                             f"got {value!r}.")
        else:
            for key in required:
                if not _finite_number(value.get(key)):
                    raise ValueError(f"asm_mate: {kind} needs a numeric value['{key}'] "
                                     f"(e.g. value={example}).")
            for key in optional:
                if value.get(key) is not None and not _finite_number(value[key]):
                    raise ValueError(f"asm_mate: {kind} value['{key}'] must be a number.")
            if (_finite_number(value.get("min")) and _finite_number(value.get("max"))
                    and value["min"] > value["max"]):
                raise ValueError(f"asm_mate: {kind} needs min <= max (got {value['min']} > "
                                 f"{value['max']}).")
    elif value is not None:
        raise ValueError(f"asm_mate: {kind} takes no value (got {value!r}); drop `value`.")
    if kind == "hinge" and isinstance(value, dict):
        has_axis = "axis" in value or "reference_axis" in value
        if has_axis != ("axis_point" in value):
            raise ValueError("asm_mate: a hinge virtual pin needs BOTH `axis` and `axis_point` "
                             "in value (WORLD coordinates).")
    return kind


def _moved_components(before: dict, after: list) -> list:
    """Ids of the components whose transform in `after` differs from `before` ({id: component})."""
    moved = []
    for comp in after:
        if not isinstance(comp, dict):
            continue
        old = (before.get(comp.get("id")) or {}).get("transform")
        new = comp.get("transform")
        if (not isinstance(old, list) or not isinstance(new, list) or len(old) != 16
                or len(new) != 16
                or any(abs(float(a) - float(b)) > 1e-9 for a, b in zip(old, new))):
            moved.append(comp.get("id"))
    return moved


# ---------------------------------------------------------------- motion (plain functions)
#
# simulate_motion and motion_fea_handoff are ordinary module-level functions. The MCP tools
# wrap them, and cad_motion_tools / cad_animation_tools import and call them directly — nothing
# reaches into FastMCP's tool registry or re-registers this family on a throwaway object.

# Mate types whose `value` is a driven dimension we can sweep to MOVE the mechanism.
# (revolute/prismatic freedom is provided by concentric/coincident/slot mates that have
# no value; you drive the DIMENSIONAL mate that parameterizes that freedom.)
_DRIVABLE_MATE_TYPES = {
    "angle": "deg",
    "distance": "mm",
}

#: Samples one sweep may take (each is a mate solve plus, when asked, an interference check).
_MOTION_MAX_STEPS = 200
#: simulate_motion returns every frame's body poses while the trajectory serialises to at most
#: this many characters; beyond it the reply is a digest (see _motion_digest).
_MOTION_FULL_TRAJECTORY_CHARS = 40_000
_MOTION_MAX_REPLY_CHARS = 250_000
_MOTION_DETAIL_MODES = ("auto", "full", "digest")


def _asm_components(doc_id: str) -> list:
    tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
    comps = tr.get("components") if isinstance(tr, dict) else None
    return comps if isinstance(comps, list) else []


def _asm_mates(doc_id: str) -> list:
    """The doc's non-suppressed mate dicts (from doc_tree)."""
    tr = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
    mates = tr.get("mates") if isinstance(tr, dict) else None
    return mates if isinstance(mates, list) else []


def _dynamics_duration(dyn: dict, tool: str) -> float:
    """dynamics.duration_s in seconds; 1.0 when absent. Zero, negative and non-finite values are
    refused — `or 1.0` once turned an explicit 0 into one second."""
    value = dyn.get("duration_s")
    if value is None:
        return 1.0
    if not _finite_number(value) or value <= 0.0:
        raise ValueError(f"{tool}: dynamics.duration_s must be a finite number of seconds > 0 "
                         f"(got {value!r}); omit it for the 1 s default.")
    return float(value)


def _mesh_fineness(value, tool: str) -> float:
    out = float(value)
    if not math.isfinite(out) or out <= 0.0:
        raise ValueError(f"{tool}: mesh_fineness must be finite and > 0.")
    return out


def _persist_motion_study(doc_id: str, mates: list, drivers: list, steps: int,
                          check_interference: bool, min_clearance_mm: float,
                          loop: bool) -> dict:
    """Record a solved sweep as the document's native Motion study so the user can open
    it in the Simulation tab and watch it play in the viewport. Only the INPUTS are stored
    (the GUI re-solves them with the same engine call); the record is keyed by its driven
    joints, so re-sweeping a joint replaces its study instead of piling up copies. Raises
    when the study cannot be stored — the caller reports that instead of omitting it."""
    labels = []
    for row in drivers:
        mate = next((m for m in mates if m.get("id") == row["mate_id"]), {})
        labels.append(str(mate.get("name") or row["mate_id"]))
    name = "Motion: " + ", ".join(labels)
    study_id = _persist_study(doc_id, {
        "type": "motion",
        "name": name,
        "motion": {
            "drivers": [{"mate_id": r["mate_id"], "start": r["start"], "stop": r["stop"]}
                        for r in drivers],
            "steps": steps,
            "loop": bool(loop),
            "fps": 24.0,
            "check_interference": check_interference,
            "min_clearance": min_clearance_mm,
        },
    }, raise_errors=True)
    if not study_id:
        raise RuntimeError("the engine stored the study but returned no study id")
    return {"doc_id": doc_id, "study_id": study_id, "name": name,
            "view": (f"Features > Assembly > Motion Study (or Simulate workspace > "
                     f"{name}) > Run motion")}


def run_motion_sweep(doc_id: Optional[str], *, mate_id: Optional[str] = None,
                     start: Optional[float] = None, stop: Optional[float] = None,
                     steps: int = 24, check_interference: bool = True,
                     min_clearance_mm: float = 0.0, loop: bool = False,
                     dynamics: Optional[dict] = None, drivers: Optional[list] = None,
                     persist_study: bool = False) -> dict:
    """The sweep behind simulate_motion, as a plain function returning the result dict.

    The trajectory is always the FULL per-frame record (the digest is a presentation of the
    simulate_motion tool). `persist_study` records the Motion study in the document; the
    read-only evidence tools (sweep_envelope, steering_turning_radius, render_animation) keep
    the default False so they never write to the caller's assembly. Raises ValueError for any
    refused argument."""
    d = _doc(doc_id)
    mates = _asm_mates(d)
    if not mates:
        raise ValueError("simulate_motion: document has no mates (not an assembly, or "
                         "no mates added). Build/open an assembly with mates first.")
    mate_id = _normalize_optional_slot(mate_id)
    if drivers is not None and (mate_id is not None or start is not None or stop is not None):
        raise ValueError(
            "simulate_motion: pass EITHER drivers=[{mate_id, start, stop}, ...] OR "
            "mate_id/start/stop — with both, the single-driver fields would be silently "
            "ignored.")
    if drivers is None:
        drivable = [m for m in mates
                    if m.get("type") in _DRIVABLE_MATE_TYPES and not m.get("suppressed")]
        if mate_id is None:
            if len(drivable) == 1:
                mate_id = drivable[0].get("id")
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
                raise ValueError(
                    "simulate_motion: multiple drivable mates — pass mate_id or drivers. "
                    f"Candidates: {ids}")
        requested = [{"mate_id": mate_id, "start": start, "stop": stop}]
    else:
        if not isinstance(drivers, list) or not drivers:
            raise ValueError("simulate_motion: drivers must be a nonempty list.")
        requested = drivers

    normalized = []
    seen = set()
    for row in requested:
        if not isinstance(row, dict):
            raise ValueError("simulate_motion: each driver must be an object.")
        mid = row.get("mate_id")
        if not isinstance(mid, str) or not mid:
            raise ValueError("simulate_motion: every driver requires a nonempty mate_id.")
        if mid in seen:
            raise ValueError(f"simulate_motion: duplicate driver mate '{mid}'.")
        seen.add(mid)
        mate = next((m for m in mates if m.get("id") == mid), None)
        if mate is None:
            raise ValueError(f"simulate_motion: mate '{mid}' not found.")
        if mate.get("suppressed"):
            raise ValueError(f"simulate_motion: mate '{mid}' is suppressed, not active.")
        if mate.get("type") not in _DRIVABLE_MATE_TYPES:
            raise ValueError(
                f"simulate_motion: mate '{mid}' is a '{mate.get('type')}' mate with "
                "no driven value to sweep. Drive a dimensional mate "
                "(" + ", ".join(sorted(_DRIVABLE_MATE_TYPES)) + ") instead.")
        bounds = {}
        for key in ("start", "stop"):
            value = row.get(key)
            if drivers is None and key == "start" and value is None:
                value = mate.get("value")
                if isinstance(value, dict):
                    value = value.get("value")
            if not _finite_number(value):
                raise ValueError(
                    f"simulate_motion: driver '{mid}' {key} must be a finite number.")
            bounds[key] = float(value)
        normalized.append({"mate_id": mid, **bounds})

    first = normalized[0]
    mate_id, start, stop = first["mate_id"], first["start"], first["stop"]
    primary_mate = next(m for m in mates if m.get("id") == mate_id)
    mtype = primary_mate["type"]
    unit = _DRIVABLE_MATE_TYPES[mtype]
    steps = int(steps)
    if steps < 2:
        raise ValueError("simulate_motion: `steps` must be >= 2.")
    if steps > _MOTION_MAX_STEPS:
        raise ValueError(
            f"simulate_motion: steps={steps} is over the {_MOTION_MAX_STEPS}-sample cap — each "
            "sample is a mate solve plus an interference check. Sweep the sub-range you need, "
            "or take fewer samples and refine near the first hit.")
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
    if drivers is not None:
        payload["drivers"] = normalized
    if dynamics is not None:
        dyn = coerce_json_or_repr(dynamics)
        if not isinstance(dyn, dict):
            raise ValueError("simulate_motion: dynamics must be an object/dict.")
        _dynamics_duration(dyn, "simulate_motion")
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
    computed = len(trajectory)
    if loop and trajectory:
        closing = copy.deepcopy(trajectory[0])
        closing["i"] = len(trajectory)
        trajectory.append(closing)
        out["trajectory"] = trajectory
        if "pose_check" in closing and "pose_check" in out:
            checked = copy.deepcopy(closing["pose_check"])
            out["pose_check"]["frames"].append({**checked, "i": closing["i"]})

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
    if persist_study and out.get("ok") and computed >= 2:
        try:
            out["motion_study"] = _persist_motion_study(
                d, mates, normalized, steps, bool(check_interference), min_clearance_mm,
                bool(loop))
        except Exception as exc:  # noqa: BLE001 — the sweep itself succeeded
            if getattr(exc, "code", None) == "engine_connection_lost":
                raise
            out["motion_study_error"] = (
                f"the sweep succeeded but its Motion study could not be saved: {exc}")
    return out


def _count(value) -> int:
    return len(value) if isinstance(value, list) else int(value) if isinstance(value, int) else 0


def _motion_digest(out: dict, reason: str) -> dict:
    """`out` with its per-frame trajectory replaced by a digest that stays small at any size.

    * frame_rows — one compact row per frame (status, residual, interference, counts), so
      EVERY frame's outcome is still inspectable;
    * worst_frames — the frames that matter (not converged, colliding, worst residual) in full
      except for their body poses;
    * per_component — each moving component's max translation, max rotation, path length and
      first/last pose relative to frame 0; components that never move are listed apart."""
    trajectory = out["trajectory"]
    rows = [{"i": fr.get("i"), "value": fr.get("value"), "converged": fr.get("converged"),
             "max_residual": fr.get("max_residual"), "interference": fr.get("interference"),
             "interference_complete": fr.get("interference_complete"),
             "collisions": _count(fr.get("collision_detail") or fr.get("collisions")),
             "pair_failures": _count(fr.get("pair_failures")),
             "geometry_failures": _count(fr.get("geometry_failures"))} for fr in trajectory]
    why: dict = {}

    def flag(pos: int, label: str) -> None:
        why.setdefault(pos, []).append(label)

    for pos, fr in enumerate(trajectory):
        if fr.get("converged") is False:
            flag(pos, "not_converged")
    hit = [p for p, fr in enumerate(trajectory) if fr.get("interference") is True]
    if hit:
        flag(hit[0], "first_interference")
        flag(max(hit, key=lambda p: rows[p]["collisions"]), "most_collisions")
        flag(hit[-1], "last_interference")
    flag(max(range(len(trajectory)),
             key=lambda p: float(trajectory[p].get("max_residual") or 0.0)), "max_residual")
    worst = [{"why": labels, **{k: v for k, v in trajectory[p].items() if k != "bodies"}}
             for p, labels in sorted(why.items())[:8]]

    def rotation_deg(a: list, b: list) -> float:
        trace = sum(a[4 * j + k] * b[4 * j + k] for j in range(3) for k in range(3))
        return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1.0) / 2.0))))

    poses: dict = {}
    names: dict = {}
    for fr in trajectory:
        for body in fr.get("bodies") or []:
            cid = str(body.get("component") or "")
            names.setdefault(cid, body.get("name"))
            poses.setdefault(cid, []).append(body["transform"])
    per_component: dict = {}
    static: list = []
    for cid, seq in poses.items():
        base = seq[0]
        origin = [base[3], base[7], base[11]]
        max_move = max_turn = path = 0.0
        prev = origin
        for m in seq[1:]:
            here = [m[3], m[7], m[11]]
            max_move = max(max_move, math.dist(here, origin))
            max_turn = max(max_turn, rotation_deg(base, m))
            path += math.dist(here, prev)
            prev = here
        if max_move <= 1e-6 and max_turn <= 1e-6:
            static.append(cid)
            continue
        per_component[cid] = {"name": names.get(cid), "max_translation_mm": max_move,
                              "max_rotation_deg": max_turn, "path_length_mm": path,
                              "first_transform": seq[0], "last_transform": seq[-1]}
    shaped = {k: v for k, v in out.items() if k != "trajectory"}
    pose_check = shaped.get("pose_check")
    if isinstance(pose_check, dict) and "frames" in pose_check:
        shaped["pose_check"] = {k: v for k, v in pose_check.items() if k != "frames"}
    dynamics = shaped.get("dynamics")
    if isinstance(dynamics, dict) and isinstance(dynamics.get("frames"), list):
        shaped["dynamics"] = {**{k: v for k, v in dynamics.items() if k != "frames"},
                              "frames_omitted": len(dynamics["frames"])}
    shaped["trajectory_mode"] = "digest"
    shaped["trajectory_digest"] = {
        "frames": len(trajectory), "reason": reason, "frame_rows": rows,
        "worst_frames": worst, "per_component": per_component,
        "static_components": sorted(static),
        "full": "re-run with detail='full' for every frame's body poses, or sweep a "
                "narrower [start, stop] / fewer steps"}
    return shaped


def _shape_motion_output(out: dict, detail: str) -> dict:
    """Apply simulate_motion's `detail` mode to a sweep result (see _motion_digest)."""
    trajectory = out.get("trajectory")
    shaped = out
    if isinstance(trajectory, list) and trajectory:
        size = len(json.dumps(trajectory, separators=(",", ":"), default=str))
        if detail == "digest":
            shaped = _motion_digest(out, "requested")
        elif detail == "auto" and size > _MOTION_FULL_TRAJECTORY_CHARS:
            shaped = _motion_digest(
                out, f"the full trajectory is {size} characters (limit "
                     f"{_MOTION_FULL_TRAJECTORY_CHARS})")
        else:
            shaped["trajectory_mode"] = "full"
    else:
        shaped["trajectory_mode"] = "full"
    if len(json.dumps(shaped, indent=2, default=str)) > _MOTION_MAX_REPLY_CHARS:
        # Do not pretend the omitted evidence is available, including for explicit full mode.
        # The sweep (and optional study save) already happened; report both facts accurately.
        result = {
            "ok": False, "reason": "motion_output_limit",
            "error": "motion evidence exceeds the reply size limit; request fewer samples "
                     "or a smaller mechanism, and use detail='digest'",
            "doc_id": out.get("doc_id"), "sweep_completed": bool(out.get("ok")),
        }
        for key in ("motion_study", "motion_study_error"):
            if key in out:
                result[key] = out[key]
        return result
    return shaped


def simulate_motion(mate_id: Optional[str] = None, start: Optional[float] = None,
                    stop: Optional[float] = None, steps: int = 24,
                    check_interference: bool = True, min_clearance_mm: float = 0.0,
                    loop: bool = False, dynamics: Optional[dict] = None,
                    doc_id: Optional[str] = None,
                    drivers: Optional[list[dict]] = None,
                    persist_study: bool = False, detail: str = "auto") -> str:
    """Sweep synchronized mate drivers; return sampled body poses and per-frame clash evidence.

    EVIDENCE / COST — read before calling:
    `ok:true` means completed, NOT collision-free. Guard `ok:false` before reading
    `trajectory`; refusals/timeouts need not contain it. Never invent missing frames.
    Inspect EVERY computed frame's collision_detail/collisions and completeness.
    Top-level interference.detail is FIRST HIT ONLY, never a union of swept pairs.
    Baseline overlaps are findings, not automatically permitted joints: justify any
    accepted pair separately. Sampled clear poses do not prove continuous path clearance.
    min_clearance_mm=0 checks overlaps only, NOT positive clearance. A positive value
    requests expensive exact distances for candidate pairs below the threshold, not a
    cheap tolerance change or a complete minimum-distance table; start with a short
    overlap sweep, then budget clearance checks deliberately.
    Print the existing result before rerunning slow work. A timeout marked
    status="still_running"/in_flight=true is not cancellation: an identical call in
    the same run joins the original work rather than restarting it.
    Procedure: cad_skill(name="simulate"), section "Mechanism proof".

    Uses the same native engine as the GUI Motion tab, not a separate Python solver.
    Each driver changes an angle/distance mate's scalar value at the same sample fraction;
    the whole mate graph is solved together so the rest of the mechanism follows.
    Reference axes follow their reference components (e.g. wheel roll follows steering).
    The previous solved pose warm-starts the next sample to preserve branch continuity.

    Scope: kinematics by default; pass `dynamics` (even {}) to ALSO run rigid-body inverse
    dynamics over the swept trajectory — mass/inertia from each component's geometry x
    density, gravity, applied forces/torques and simple viscous damping, with per-mate JOINT
    REACTION forces/moments solved through the mate constraint jacobian. Tangent/cam mates
    contribute their instantaneous normal reaction only; unilateral lift-off/impact/contact
    integration remains out of scope.

    dynamics : None (default) = kinematics only. A dict (may be empty — all keys
              optional) adds a `dynamics` block:
              {duration_s (sweep wall-time, default 1.0; must be > 0), gravity:[3] m/s²
              (default [0,0,-9.81]),
              densities:{component_id: g/cm³}, masses:{component_id: kg},
              default_density_g_cm3 (only for components with NO applied material),
              loads:[{component, force_n:[3], torque_nm:[3]}],
              damping:{linear_ns_per_m, angular_nms_per_rad}}.
              Each component's density is, most specific first: masses[id] (density follows),
              densities[id], the material applied to its source part (set_material), then
              default_density_g_cm3. If none is available, dynamics refuses rather than
              guessing. `dynamics.bodies[]` names each component's `density_source` and
              `material_key`.
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
              An angle drive requires a hinge LINE: value.axis + value.axis_point
              (first ref's local frame), a third axial ref on the angle mate, or
              a concentric/hinge mate on the driven body. A scalar angle alone
              cannot define a pivot and is refused as driver_axis_line_unresolved.
    start,stop : the value range to sweep (in the mate's unit — deg or mm). Omit `start` to
              begin at the mate's current value. `stop` is required.
    drivers : optional nonempty list [{mate_id, start, stop}, ...] INSTEAD of
              mate_id/start/stop (passing both is an error). Each row requires a
              unique, active angle/distance mate ID and finite numeric bounds (degrees
              for angle, mm for distance); no auto-pick or omitted bounds inside rows.
              All drivers share `steps` and the same normalized sample timeline.
    steps   : number of SAMPLES across [start, stop] inclusive (>=2, >=3 with dynamics,
              at most 200).
              More samples improve finite-difference accuracy and branch continuity.
    loop    : if True, append the original start pose, driver_values and check outcomes
              for playback only. No additional pose or interference check is computed;
              neither frames_checked nor the dynamics timeline includes this copy. The
              saved Motion study records this flag as passed.
    check_interference : evaluate every valid sample for solid overlap or
              below-minimum-clearance pairs. min_clearance_mm is in mm; exact
              distances may be expensive even with unchanged-relative-pair reuse.
    persist_study : False (default) leaves the document untouched. True records the sweep's
              INPUTS as the native Motion study (`motion_study` {doc_id, study_id, name, view};
              re-sweeping the same joints replaces it). A failed save is reported in
              `motion_study_error`, never dropped.
    detail  : "auto" (default) returns every frame's body poses while the trajectory is small
              (<= 40000 characters) and a DIGEST beyond that; "full" requests every
              frame; "digest" requests the digest. Replies exceeding 250000 characters
              are refused with motion_output_limit rather than silently truncating evidence.
              The digest
              (`trajectory_digest`, `trajectory_mode:"digest"`, no `trajectory`) holds one
              status row per frame (frame_rows: converged, residual, interference,
              collision count), the worst frames with their collision detail, and each
              moving component's max translation/rotation, path length and first/last pose
              (per_component); `dynamics.frames` is dropped to its summaries. Every frame's
              outcome stays inspectable in frame_rows; only the per-frame poses are omitted.

    Returns {ok, doc_id, mate_id, driver:{type,unit,start,stop,steps,samples},
             drivers:[{mate_id,type,unit,start,stop,steps}],
             trajectory:[{i,value,driver_values:{mate_id:value},dof,residual_dof,
                          converged,max_residual,worst_mate,tolerance,pose_check,
                          bodies:[{component,name,transform}],collisions,
                          collision_detail,interference,interference_complete,
                          pairs_checked,pairs_reused,pair_failures,geometry_failures}],
             trajectory_mode, interference:{checked,complete,frames_checked,pair_failures,
                           geometry_failures,first_collision_step,
                           first_collision_value,detail},
             summary, pose_check, reference_bodies, reference_transforms}.
    mate_id, driver and frame.value describe the first driver for compatibility.
    Every body transform is world 4x4 (row-major 16); reference_transforms are
    already row-major and are not transposed. Frame pose_check validates each
    driven body's relative motion against its hinge line; top-level pose_check
    aggregates those frames. Failed poses make ok=false and skip that frame's check.
    collision_detail contains all known overlap/unsafe-clearance pairs for that
    frame, with stable component_a/component_b, overlap_volume_mm3 for overlaps,
    or distance (mm) and warning="below min clearance" for clearance hits.
    collisions contains the same hits with overlap_volume_mm3 defaulted to zero,
    so a zero-volume entry can still violate clearance. A pair's reused=true and
    pairs_reused count indicate cached results at an unchanged relative pose;
    pairs_checked includes these classified pairs, not just newly computed ones.
    Frame interference is true for known hits, false for a complete clear check,
    or null when unchecked/incomplete without a known hit. Known hits survive an
    incomplete check. interference_complete, pair_failures and geometry_failures
    expose gaps; check field presence on unchecked frames before indexing counts.
    A sampled-clear claim requires check_interference=true, interference.complete
    true, frames_checked==steps, all computed frames converged/pose-valid, and no
    hits in any frame. The loop playback copy is not another computed frame.
    The engine solves an immutable snapshot; live poses, mates and history never change.
    When you report a motion check and a Motion study was saved, tell the user where to
    watch it: its `view` path.
    """
    try:
        if detail not in _MOTION_DETAIL_MODES:
            raise ValueError(f"simulate_motion: detail must be one of "
                             f"{', '.join(_MOTION_DETAIL_MODES)}; got {detail!r}.")
        out = run_motion_sweep(
            doc_id, mate_id=mate_id, start=start, stop=stop, steps=steps,
            check_interference=check_interference, min_clearance_mm=min_clearance_mm,
            loop=loop, dynamics=dynamics, drivers=drivers, persist_study=bool(persist_study))
        return _dumps(_shape_motion_output(out, detail))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ------------------------------------------------------------------ motion -> FEA handoff

class _HandoffRefused(Exception):
    """A refusal that is the tool's own ok:false reply (it carries the payload)."""

    def __init__(self, payload: dict):
        super().__init__(str(payload.get("error") or "motion_fea_handoff refused"))
        self.payload = payload


def _finite_vec(raw, label: str) -> list:
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError(f"motion_fea_handoff: {label} must have 3 components.")
    vec = [float(x) for x in raw]
    if not all(math.isfinite(x) for x in vec):
        raise ValueError(f"motion_fea_handoff: {label} must be finite.")
    return vec


def _face_centroid(selector) -> Optional[list]:
    if not isinstance(selector, dict) or selector.get("kind") != "face":
        return None
    raw = selector.get("centroid")
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and
               math.isfinite(float(x)) for x in raw):
        return None
    return [float(x) for x in raw]


def _same_face(left: dict, right: dict) -> bool:
    left_sid, right_sid = left.get("sid"), right.get("sid")
    if left_sid and right_sid:
        return left_sid == right_sid
    lc, rc = _face_centroid(left), _face_centroid(right)
    if lc is None or rc is None:
        return False
    if math.sqrt(sum((a - b) ** 2 for a, b in zip(lc, rc))) > 1e-6:
        return False
    ln, rn = left.get("normal"), right.get("normal")
    if (isinstance(ln, list) and len(ln) == 3 and
            isinstance(rn, list) and len(rn) == 3):
        ln, rn = _finite_vec(ln, "fixture face normal"), _finite_vec(rn, "mate face normal")
        lmag = math.sqrt(sum(x * x for x in ln))
        rmag = math.sqrt(sum(x * x for x in rn))
        if lmag <= 1e-12 or rmag <= 1e-12:
            return False
        if sum(a * b for a, b in zip(ln, rn)) / (lmag * rmag) < 1.0 - 1e-6:
            return False
    return True


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


def _frame_peak(ctx, frame: dict) -> float:
    """Largest reaction force (N) any joint on the isolated component carries in `frame`."""
    peak = 0.0
    for reaction in frame.get("reactions") or []:
        if reaction.get("mate_id") not in ctx.touch_mids:
            continue
        peak = max(peak, _norm(_finite_vec(reaction.get("force_n"), "reaction force_n")))
    return peak


@contextmanager
def handoff_setup(d: str, component: str, fixtures, *, mate_id, start, stop, steps,
                  dynamics, material, fixture_mate_id):
    """The part of motion_fea_handoff that does not depend on WHICH frame is analysed.

    Runs the inverse-dynamics motion sweep ONCE, isolates the component, and resolves its
    fixture face and every joint face on a PRIVATE document of its source part
    (open_component_part: the live open document when there is one, otherwise an invisible,
    path-detached copy that is closed on exit — never a tab in the user's GUI). Yields the
    context handoff_frame turns into loads for any frame; the sweep's frames are shared by
    every frame analysed under it. Raises _HandoffRefused for the tool's ok:false refusals."""
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
        raise ValueError("motion_fea_handoff: steps must be >= 3 for acceleration dynamics.")
    if steps > _MOTION_MAX_STEPS:
        raise ValueError(f"motion_fea_handoff: steps={steps} is over the {_MOTION_MAX_STEPS}-"
                         "sample cap.")
    dyn_opts = coerce_json_or_repr({} if dynamics is None else dynamics)
    if not isinstance(dyn_opts, dict):
        raise ValueError("motion_fea_handoff: dynamics must be an object/dict.")
    _dynamics_duration(dyn_opts, "motion_fea_handoff")
    # A component with no applied material takes the FEA material's density — never a
    # hard-coded steel. Components that DO have a material keep it (the engine reads it).
    try:
        fea_density = _resolve_material_density(material)
    except ValueError:
        # A stiffness-only material is valid when every component has an applied
        # material, explicit mass, or explicit density. The engine rejects any gap.
        fea_density = None
    if "default_density_g_cm3" not in dyn_opts and fea_density is not None:
        dyn_opts = {**dyn_opts, "default_density_g_cm3": fea_density}

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
    if start is None:
        value = driven.get("value")
        if isinstance(value, dict):
            value = value.get("value")
        if not _finite_number(value):
            raise ValueError(
                f"motion_fea_handoff: mate '{mate_id}' has no finite numeric value; "
                "pass `start`.")
        start = float(value)
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
    src_path = ""
    if (comp.get("source") or {}).get("path"):
        src_path = component_source_file(comp, d)
    if not src_path:
        raise ValueError(f"motion_fea_handoff: component '{cid}' has no source path.")

    with open_component_part(src_path) as part_doc:
        fixture_selector = _resolve_one_selector(part_doc, fixtures[0], kind="face")
        fixture_sets = _resolve_face_points(part_doc, fixture_selector)
        if _face_centroid(fixture_selector) is None:
            raise ValueError(
                "motion_fea_handoff: fixture query must resolve one stable face with "
                "a finite centroid; tied/predicate-only fixture queries are ambiguous.")

        # --- motion study WITH dynamics (engine-side immutable sweep, run once) ------
        r_mot = cb.call("simulate_motion", {
            "doc_id": d, "mate_id": mate_id, "start": start,
            "stop": stop, "steps": steps,
            "check_interference": False, "min_clearance_mm": 0.0,
            "dynamics": dyn_opts})
        dyn = r_mot.get("dynamics") or {}
        if dyn and (not dyn.get("ok") or not dyn.get("force_path_resolved")):
            raise _HandoffRefused({
                "ok": False,
                "error": "motion dynamics has an unresolved force/moment path; "
                         "refusing to create FEA loads",
                "warnings": dyn.get("warnings")})
        if not r_mot.get("ok"):
            raise _HandoffRefused({
                "ok": False,
                "error": r_mot.get("error", "motion sweep did not converge"),
                "warnings": dyn.get("warnings")})
        frames = dyn.get("frames") or []
        if not frames or not all(fr.get("balanced") for fr in frames):
            raise _HandoffRefused({
                "ok": False,
                "error": "motion dynamics produced no fully balanced frame set",
                "warnings": dyn.get("warnings")})

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
            raise _HandoffRefused({
                "ok": False,
                "error": f"no solved joint reactions touch component '{cid}'",
                "warnings": dyn.get("warnings")})
        bad_refs = [mid for mid in sorted(touch_mids)
                    if len(component_refs.get(mid) or []) != 1]
        if bad_refs:
            raise _HandoffRefused({
                "ok": False,
                "error": "each solved joint on the isolated component must have exactly "
                         f"one component-side face reference; invalid mates: {bad_refs}",
                "warnings": dyn.get("warnings")})
        selectors = {mid: component_refs[mid][0].get("selector") or {}
                     for mid in touch_mids}
        invalid_faces = [mid for mid, selector in selectors.items()
                         if _face_centroid(selector) is None]
        if invalid_faces:
            raise _HandoffRefused({
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

        fixture_candidates = sorted(mid for mid in touch_mids if _is_grounded_fixture(mid))
        setup_diagnostics = {
            "fixture_face": fixture_selector,
            "fixture_mate_candidates": fixture_candidates,
            "fixture_mate_id": fixture_mate_id,
            "fixture_reaction_omitted_from_loads": False,
            "body_load_face": None,
        }
        if len(fixture_candidates) != 1:
            raise _HandoffRefused({
                "ok": False,
                "error": "fixture face must identify exactly one solved mate to a "
                         "fixed component; duplicate or missing grounded mates are an "
                         f"ambiguous boundary (candidates: {fixture_candidates})",
                "setup_diagnostics": setup_diagnostics,
                "warnings": dyn.get("warnings")})
        resolved_fixture_mate_id = fixture_candidates[0]
        if fixture_mate_id is not None and fixture_mate_id != resolved_fixture_mate_id:
            raise _HandoffRefused({
                "ok": False,
                "error": f"fixture_mate_id '{fixture_mate_id}' is not the solved mate "
                         "joining the requested fixture face to a fixed component",
                "setup_diagnostics": setup_diagnostics,
                "warnings": dyn.get("warnings")})
        fixture_mate_id = resolved_fixture_mate_id
        setup_diagnostics["fixture_mate_id"] = fixture_mate_id
        setup_diagnostics["fixture_reaction_omitted_from_loads"] = True

        # Where each component's inertial mass came from — never an unstated steel.
        density_sources = {
            str(b.get("component")): {"density_g_cm3": b.get("density_g_cm3"),
                                      "source": b.get("density_source"),
                                      "material_key": b.get("material_key")}
            for b in (dyn.get("bodies") or []) if isinstance(b, dict)}
        density_warnings = []
        own = density_sources.get(str(cid)) or {}
        own_density = own.get("density_g_cm3")
        if (own.get("source") == "component_material" and fea_density
                and _finite_number(own_density)
                and abs(own_density - fea_density) > 0.01 * fea_density):
            density_warnings.append(
                f"component '{cid}' inertia uses its applied material "
                f"({own.get('material_key')}, {own_density:.4g} g/cm^3) but the FEA solve uses "
                f"material {material if isinstance(material, str) else 'custom'} "
                f"({fea_density:.4g} g/cm^3); pass material=<the applied material> so the "
                "stress and the inertial loads describe the same part")

        ctx = SimpleNamespace(
            doc=d, cid=cid, comp=comp, src_path=src_path, part_doc=part_doc,
            driven_mate=r_mot.get("mate_id"), r_mot=r_mot, dyn=dyn, frames=frames,
            touch_mids=touch_mids, selectors=selectors, joint_meta=joint_meta,
            fixture_selector=fixture_selector, fixture_sets=fixture_sets,
            fixture_mate_id=fixture_mate_id, setup_diagnostics=setup_diagnostics,
            density_sources=density_sources, density_warnings=density_warnings, step=None)
        try:
            yield ctx
        finally:
            if ctx.step:
                try:
                    os.unlink(ctx.step)
                except OSError:
                    pass


def select_handoff_frame(ctx, at) -> dict:
    """The sweep frame `at` names: "worst" (largest joint reaction on the component) or a time."""
    frames = ctx.frames
    if isinstance(at, str):
        if at != "worst":
            raise ValueError("motion_fea_handoff: at must be 'worst' or a time in s.")
        return max(frames, key=lambda fr: _frame_peak(ctx, fr))
    t_want = float(at)
    if not math.isfinite(t_want):
        raise ValueError("motion_fea_handoff: at time must be finite.")
    t_min = float(frames[0].get("t_s", 0.0))
    t_max = float(frames[-1].get("t_s", 0.0))
    if t_want < t_min or t_want > t_max:
        raise ValueError(
            f"motion_fea_handoff: at={t_want:g}s is outside [{t_min:g},{t_max:g}]s.")
    return min(frames, key=lambda fr: abs(float(fr.get("t_s", 0.0)) - t_want))


def handoff_frame(ctx, frame: dict, *, at, body_load_face, material, mesh_fineness: float,
                  dry_run: bool) -> dict:
    """Loads, balance checks and (unless dry_run) the static FEA for ONE frame of a handoff
    setup. Raises _HandoffRefused for an ok:false refusal; returns the result dict."""
    d, cid, dyn, part_doc = ctx.doc, ctx.cid, ctx.dyn, ctx.part_doc
    selectors, joint_meta = ctx.selectors, ctx.joint_meta
    fixture_selector, fixture_mate_id = ctx.fixture_selector, ctx.fixture_mate_id
    setup_diagnostics = dict(ctx.setup_diagnostics)
    fi, ft = int(frame.get("i", 0)), float(frame.get("t_s", 0.0))
    if _frame_peak(ctx, frame) <= 1e-12:
        raise _HandoffRefused({
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
        (fr for fr in (ctx.r_mot.get("trajectory") or [])
         if int(fr.get("i", -1)) == fi), None)
    pose = next(
        (b.get("transform") for b in ((trajectory_frame or {}).get("bodies") or [])
         if b.get("component") == cid), None)
    if not isinstance(pose, list) or len(pose) != 16:
        raise ValueError("motion_fea_handoff: selected component pose is unavailable.")
    pose = [float(x) for x in pose]  # engine convention: column-major
    if not all(math.isfinite(x) for x in pose):
        raise ValueError("motion_fea_handoff: selected component pose is non-finite.")

    def _world_to_local(vec):
        return [sum(pose[c * 4 + r] * vec[r] for r in range(3)) for c in range(3)]

    reaction_rows = []
    for reaction in frame.get("reactions") or []:
        mid = reaction.get("mate_id")
        if mid not in ctx.touch_mids:
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
        centroid = _face_centroid(selectors[mid])
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
        raise _HandoffRefused({
            "ok": False,
            "error": f"selected frame has no reaction for fixture mate '{fixture_mate_id}'",
            "setup_diagnostics": setup_diagnostics,
            "warnings": dyn.get("warnings")})

    zero = [0.0, 0.0, 0.0]
    joint_force_sum = list(zero)
    joint_moment_sum = list(zero)
    for row in reaction_rows:
        joint_force_sum = _vadd(joint_force_sum, row["force_local_n"])
        joint_moment_sum = _vadd(joint_moment_sum, row["wrench_moment_about_origin_local_nm"])
    body_force = _vneg(joint_force_sum)
    body_moment = _vneg(joint_moment_sum)
    moment_scale = max(
        [1.0, _norm(body_moment)] +
        [_norm(row["wrench_moment_about_origin_local_nm"]) for row in reaction_rows])
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
        {"mate_id": row["mate_id"], "moment_local_nm": row["joint_moment_local_nm"]}
        for row in reaction_rows
        if row["mate_id"] != fixture_mate_id and
        _norm(row["joint_moment_local_nm"]) > moment_tol
    ]
    if unsupported_couples:
        balance_diagnostics["unsupported_nonfixture_joint_couples"] = unsupported_couples
        raise _HandoffRefused({
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
    warnings = list(dyn.get("warnings") or []) + list(ctx.density_warnings)
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
        represented_joint_force = _vadd(represented_joint_force, row["force_local_n"])
        represented_joint_moment = _vadd(
            represented_joint_moment,
            _cross([x * 1e-3 for x in row["centroid_mm"]], row["force_local_n"]))

    represented_body_moment = list(zero)
    if _norm(body_force) > force_tol:
        if body_load_face is None:
            balance_diagnostics["body_load_representation_complete"] = False
            raise _HandoffRefused({
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
            raise _HandoffRefused({
                "ok": False,
                "error": "body_load_face must be distinct from the fixed fixture face; "
                         "a load on constrained nodes would not stress the component",
                "setup_diagnostics": setup_diagnostics,
                "balance_diagnostics": balance_diagnostics,
                "warnings": warnings})
        body_centroid = _face_centroid(body_selector)
        if body_centroid is None:
            raise ValueError(
                "motion_fea_handoff: body_load_face lacks a finite face centroid.")
        represented_body_moment = _cross([x * 1e-3 for x in body_centroid], body_force)
        body_moment_error = _vsub(represented_body_moment, body_moment)
        if _norm(body_moment_error) > moment_tol:
            balance_diagnostics.update({
                "body_load_representation_complete": False,
                "body_load_face_moment_local_nm": represented_body_moment,
                "body_load_moment_error_nm": _norm(body_moment_error),
                "body_load_moment_tolerance_nm": moment_tol,
            })
            raise _HandoffRefused({
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
        raise _HandoffRefused({
            "ok": False,
            "error": "inverse dynamics requires a pure equivalent body/inertial "
                     "couple, which the face-force static solver cannot apply",
            "setup_diagnostics": setup_diagnostics,
            "balance_diagnostics": balance_diagnostics,
            "warnings": warnings})

    if not loads:
        raise _HandoffRefused({
            "ok": False,
            "error": "isolated snapshot has no non-zero representable load after "
                     "omitting the fixture reaction",
            "setup_diagnostics": setup_diagnostics,
            "balance_diagnostics": balance_diagnostics,
            "warnings": warnings})

    applied_force = _vadd(represented_joint_force, body_force)
    applied_moment = _vadd(represented_joint_moment, represented_body_moment)
    force_closure = _vadd(applied_force, fixture_reaction["force_local_n"])
    moment_closure = _vadd(
        applied_moment, fixture_reaction["wrench_moment_about_origin_local_nm"])
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
    if _norm(force_closure) > force_tol or _norm(moment_closure) > moment_tol:
        raise _HandoffRefused({
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
            raise _HandoffRefused({
                "ok": False,
                "error": f"{load['source']} face did not resolve on the component part",
                "setup_diagnostics": setup_diagnostics,
                "balance_diagnostics": balance_diagnostics,
                "warnings": warnings})
        load_specs.append({"faces": faces, "type": "force", "vector": load["force"]})

    env = sh.build_handoff(
        "motion_to_fea",
        sh.study_ref("motion", "simulate_motion", doc_id=d,
                     result_digest={"driver_mate": ctx.driven_mate,
                                    "worst_case": dyn.get("worst_case"),
                                    "picked_frame": {"i": fi, "t_s": ft}}),
        sh.study_ref("fea", "run_fea_static"),
        geometry=[sh.geometry_ref("component", "doc", path=ctx.src_path, units="mm",
                                  derived_by="assembly component source",
                                  meta={"component": cid, "name": ctx.comp.get("name")})],
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
        "source_path": ctx.src_path,
        "frame": {"i": fi, "t_s": ft},
        "loads_applied": loads,
        "setup_diagnostics": setup_diagnostics,
        "balance_diagnostics": balance_diagnostics,
        "density_sources": ctx.density_sources,
        "warnings": warnings,
        "fea": None,
    }
    if dry_run:
        result["note"] = "dry_run: balanced loads + envelope built, FEA solve skipped"
        return sh.attach(result, env)

    mat = _resolve_material(material)
    if ctx.step is None:
        # One export of the component serves every frame analysed under this setup.
        ctx.step = _tmp(".step")
        cb.call("doc_export_step", {"doc_id": part_doc, "path": ctx.step})
    study = {"step_path": ctx.step, "material": mat, "fixtures": ctx.fixture_sets,
             "loads": load_specs, "mesh_order": 2, "fineness": mesh_fineness}
    res = cb.call("sim_run_static", {"study": study})
    if not res.get("ok"):
        result["ok"] = False
        result["fea"] = {"ok": False, "error": res.get("error", "solve failed")}
        return sh.attach(result, env)
    result["fea"] = {"ok": True, "summary": res.get("summary", {}),
                     "n_dof": res.get("n_dof"),
                     "material": mat.get("name", material)}
    return sh.attach(result, env)


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

    Inertial mass comes from each component's APPLIED material (set_material on its part);
    a component with none takes the density of ``material`` (the FEA material). The reply's
    ``density_sources`` names each component's density and where it came from, and warns when
    the analysed component's applied material differs from ``material``. It never assumes
    steel. ``dynamics.default_density_g_cm3`` / ``densities`` / ``masses`` override as in
    simulate_motion.

    The component's part is read through a private, invisible document (or the live open
    one) — no tab opens in the GUI, and no study is written into the part: the numbers and the
    ``simhandoff`` envelope are the evidence.

    Returns the applied local-N loads plus ``setup_diagnostics`` and
    ``balance_diagnostics``.  ``dry_run`` performs every setup/balance check and
    builds the handoff envelope but skips the FEA solve.
    """
    try:
        d = _doc(doc_id)
        mesh_fineness = _mesh_fineness(mesh_fineness, "motion_fea_handoff")
        with handoff_setup(d, component, fixtures, mate_id=mate_id, start=start, stop=stop,
                           steps=steps, dynamics=dynamics, material=material,
                           fixture_mate_id=fixture_mate_id) as ctx:
            frame = select_handoff_frame(ctx, at)
            return _dumps(handoff_frame(
                ctx, frame, at=at, body_load_face=body_load_face, material=material,
                mesh_fineness=mesh_fineness, dry_run=dry_run))
    except _HandoffRefused as refusal:
        return _dumps(refusal.payload)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def register_cad_asm_tools(mcp: FastMCP) -> None:
    """CAD assembly + motion MCP tools (split out of cad_tools.py): new_assembly, asm_add_component, asm_mate, solve_mates, simulate_motion, motion_fea_handoff, list_world_bodies."""


    # ---------------------------------------------------------------- assembly






    def _input_component_candidates(path: str) -> list:
        """Resolve a caller-typed path against exactly the selected workspace.

        Stored component sources use component_source_base instead: a saved assembly's
        folder is not a fallback for project-relative input, nor is the worker cwd.
        """
        return [resolve_agent_path(path, "path")]





    def _components_phrase(count) -> str:
        if isinstance(count, int):
            return f"{count} component{'' if count == 1 else 's'}"
        return "its existing components"



    @mcp.tool()
    def new_assembly(name: str = "assembly", *, fresh: bool = False) -> str:
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
        A resumed assembly reports `reused: true` and its `component_count` — it still holds
        the components it had; add to them, don't re-add them.

        `fresh=True` is the ONLY way to get a separate EMPTY assembly while one of that name
        (or version series) is open: it skips both resumes and the engine numbers the new
        document if the name is taken. Use it for a deliberately separate assembly, not to
        iterate on the same design.
        """
        try:
            docs = _resumable_docs()
            series = (None if fresh else
                      _pick_best_doc(_docs_in_version_series(docs, name, kind="assembly")))
            if series is not None:
                did = series["doc_id"]
                _CUR["doc_id"] = did
                claim_process_doc(did)
                actual = series.get("name") or name
                count = series.get("component_count")
                return _dumps({
                    "ok": True, "doc_id": did, "name": actual, "kind": "assembly",
                    "revision": series.get("revision", 0),
                    "reused": True, "resumed": True, "resumed_by": "version_series",
                    "version_series": _version_series_base(name),
                    "requested_name": name,
                    "component_count": count,
                    "note": f'"{name}" is the next VERSION of the open assembly '
                            f'"{actual}" (doc_id={did}), not a separate assembly — '
                            f"resumed it ({_components_phrase(count)}) so the design stays "
                            f"ONE document with ONE version history (plm_timeline / "
                            f'plm_rollback). Edit it in place and keep calling it "{actual}"; '
                            f"call new_assembly(name, fresh=True) only for a deliberately "
                            f"separate empty assembly."})
            # resume_if_open makes the engine decide check-and-mint under its mint lock,
            # so two callers racing on one name get one document (the same contract
            # new_part relies on — see cad_doc_tools.new_part and core/doc/session.cpp).
            # Verbatim when an assembly of that exact name is already open — that is the
            # engine's resume_if_open match and it keys on the name as given, so stripping
            # the token would miss it and mint a duplicate (same guard as new_part).
            exact_open = [e for e in _docs_named(docs, name)
                          if (e.get("kind") or "part") == "assembly"]
            mint_name = name if (exact_open and not fresh) else _strip_version_suffix(name)
            r = cb.call("doc_new", {"kind": "assembly", "name": mint_name,
                                    "resume_if_open": not fresh, **_doc_new_scope()})
            did = r.get("doc_id")
            _CUR["doc_id"] = did
            claim_process_doc(did)
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
            if fresh:
                out["fresh"] = True
                if actual != name:
                    out["requested_name"] = name
                    out["note"] = (
                        f'an assembly named "{name}" was already open, so the engine '
                        f'numbered this fresh empty document "{actual}"')
            if r.get("reused"):
                # doc_new's resume reply carries feature_count, not component_count;
                # the doc_list row fetched above has it.
                count = r.get("component_count")
                if count is None:
                    count = next((e.get("component_count") for e in docs
                                  if e.get("doc_id") == did), None)
                out.update(reused=True, resumed=True, component_count=count,
                           note=f'RESUMED the existing open assembly "{out["name"]}" '
                                f"(doc_id={did}), which already has "
                                f"{_components_phrase(count)} — new asm_add_component calls "
                                f"ADD to them. For a separate empty assembly call "
                                f'new_assembly("{name}", fresh=True).')
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def asm_add_component(path: str, name: Optional[str] = None, fixed: Optional[bool] = None,
                          at: Optional[list] = None, rotate: Optional[list] = None,
                          angle: float = 0.0, doc_id: Optional[str] = None) -> str:
        """Insert a saved part file into the current assembly as a component.

        path  : the part file to place (.step+ preferred — editable; plain STEP accepted).
                A relative path is PROJECT-ROOT relative (like fs_*/save_part), e.g.
                "parts/bracket.step+", wherever the assembly file itself lives.
        name  : component display name (default: the file stem).
        fixed : ground this component (default: the FIRST component is fixed, later ones free).
        at    : optional [x,y,z] translation in mm, in the ASSEMBLY (world) frame: where the
                part file's own origin lands. A STARTING POSE for the mate solver, or a
                MEASURED pose when you are placing without mates.
        rotate/angle : optional rotation axis [ax,ay,az] (assembly frame; any nonzero
                length — it is normalised) and `angle` in DEGREES, right-hand rule about
                that axis through the part's own origin. Composition: the rotation is
                applied FIRST, then `at`: world = R(rotate, angle) · p_part + at — so
                rotating never moves where the part origin lands. The pose is absolute
                (the component's full transform), not added to a previous one.
                `angle` needs a `rotate` axis and `rotate` needs a nonzero `angle`; a
                zero-length axis is refused (each used to place the component wrongly and
                still report ok).
        Returns the new component id ("k1", "k2", ...) for use in asm_mate refs, plus
        `placement_source` ("transform" when you passed at/rotate, "mate" once a mate on this
        component solves). Provenance is saved inside the .step+ document and survives reopen.
        Use add_features with a transform feature and body:<component_id> to compose a
        measured world-space move with its placement (copy:true alone duplicates).
        Mate-owned poses refuse. set_suppressed with that component id suppresses/restores
        it; feature ids are only valid suppression targets on part documents. To rename a
        placed component, add_features on the assembly with
        {"op":"rename","id":"k3","name":"LeftWheel"} (or
        {"op":"asm_edit_component","id":"k3","name":"LeftWheel"}); `name` here sets it at
        insert time.

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
            d = _doc(doc_id)
            transform = _component_pose(at, rotate, angle)
            candidates = _input_component_candidates(path)
            p = next((c for c in candidates if os.path.exists(c)), None)
            if p is None:
                return _dumps({"ok": False,
                               "error": f"no such file: {path!r} (tried: "
                                        f"{', '.join(candidates)}). A relative path is "
                                        "project-root relative; pass the path save_part "
                                        "reported, or an absolute path.",
                               "tried": candidates})
            op: dict = {"op": "asm_insert_component", "source": {"path": p}}
            if name:
                op["name"] = name
            if fixed is not None:
                op["fixed"] = bool(fixed)
            if transform is not None:
                # row-major 4x4: rotation about `rotate` by `angle` deg, then translate `at`.
                # Same helper add_features' asm_edit_component re-pose path uses, so an
                # insert pose and a later re-pose can never disagree on the convention.
                op["transform"] = transform
            r = cb.call("doc_apply_ops", {"doc_id": d,
                                          "with_tess": False, "ops": [op]})
            kid = (r.get("id_map") or {}).get("$0")
            comps = _asm_components(d)
            me = next((c for c in comps if c.get("id") == kid), {})
            # Placement provenance: a raw at/rotate pose is only as good as the measurement
            # behind it, so it is stamped "transform" and stays unverified until
            # check_interference runs. An identity insert is "origin" (nothing was asserted
            # about where it sits); asm_mate/solve_mates upgrade either to "mate".
            src = "transform" if transform is not None else "origin"
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
                    f"or a bare component id) — got a {len(v)}-item list. `a` and `b` are each "
                    f"ONE face/edge of ONE component; further operands go in extra=[...].")
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
                for key in ("query", "sid"):
                    if key in v:
                        out[key] = v[key]
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
                 value: Optional[Union[float, dict]] = None,
                 align: str = "aligned", solve: bool = True,
                 doc_id: Optional[str] = None,
                 extra: Optional[list] = None) -> str:
        """Add a MATE between components of the current assembly (and solve it).

        mate_type : one of the engine's 19 types — anything else is refused with this list.
                    `a`/`b` are the first two operands, `value` where listed, `extra` for more:
                      coincident | parallel | perpendicular | tangent | concentric | lock
                          no value. Seat a bolt with concentric (shank <-> bore) + coincident
                          (head seat <-> plate face).
                      distance (mm) | angle (deg)
                          `value` = the number (or an object with a numeric "value").
                          angle takes ONE optional extra axial operand, or a hinge line in
                          `value` (below).
                      distance_limit | angle_limit
                          value={"min":..,"max":..} (mm / deg; optional "value" = current
                          setting). Keeps the gap / angle between a and b inside the range.
                      hinge
                          Revolute joint: the axes of a and b coincide. `value` optional:
                          {"min":deg,"max":deg} swing limits (default 0..360) and/or the
                          virtual pin {"axis":[x,y,z],"axis_point":[x,y,z]} (WORLD coords; both
                          or neither). Up to 2 extra operands: direction references that
                          define zero swing.
                      slot
                          a's pin/point stays on b's slot line (b = a linear edge or axis).
                      profile_center
                          the centre of a coincides with the centre of b (circles/cylinders).
                      width
                          centres a tab (a) between two jaw planes: b = jaw 1, extra=[jaw 2]
                          (an optional 2nd extra operand is accepted). No value.
                      symmetric
                          a and b mirror each other about a plane: extra=[symmetry plane].
                      gear
                          value={"ratio": r}: rotation of b = -r x rotation of a about their
                          axes (align "anti" = same direction).
                      rack_pinion
                          a = pinion (round face/axis), b = rack (its direction axis); value
                          optional {"pitch_radius": mm} (default: a's radius). A turn of a
                          moves b along its axis by radius x angle.
                      screw
                          value={"pitch": mm per revolution}: axial travel = pitch/2pi x the
                          relative rotation of b about a.
                      cam_follower
                          a = follower (circular edge/point with a radius), b = cam (cylinder
                          axis with a radius): the follower stays in contact with the cam.
                    Types that take no value refuse one. Flip `align` if a coupled or limited
                    part moves the wrong way.
        a, b      : each names ONE face/edge of ONE component:
                      {"component": "k1", "query": {"of":"face","side":"top"}}
                    `query` uses the structured geometry query keys from query_geometry,
                    resolved against that component's SOURCE part (the live open document of
                    that file when it is open, saved or not); a bare side word
                    ("top") also works. It must resolve to exactly ONE face/edge;
                    narrow with pick/near/count if it matches several. A face/edge sid
                    ("F2bb10f" — describe_part/query_geometry/render tiles hand them out)
                    names ONE element outright: {"component": "k1", "query": "F2bb10f"},
                    {"component": "k1", "query": {"sid": "F2bb10f"}} or
                    {"component": "k1", "sid": "F2bb10f"}. A sid read off the component's
                    source part or off this assembly (posed instance) both resolve.
                    To replace an existing mate's operands, use add_features with
                    {"op":"asm_edit_mate","id":"m1","refs":[a,b]}; each operand
                    accepts the same {component, query|sid} forms and resolves against
                    its component's source part.
        extra     : further operands in the same forms, for the types above that take them
                    (width, symmetric, hinge, angle). The count must match the type.
        align     : "aligned" or "anti". For planar coincident, aligned puts outward
                    normals opposite and anti puts them in the same direction; for
                    parallel, aligned means same and anti opposite. Planar distance
                    uses normal·(point_b - point_a) = sign*value, with sign +1 for
                    aligned and -1 for anti. The normal belongs to a if a is a
                    plane, otherwise b. These are the existing saved-mate semantics.
        value     : see mate_type. For an angle-only hinge, pass
                    {"value":0, "axis":[0,1,0], "axis_point":[x,y,z]};
                    axis and axis_point are in WORLD coordinates; they are stored in
                    component a's local frame so the joint survives save/reopen.
                    If a face normal lies along the pin, the authored body orientation
                    defines zero angle (no concentric mate or rear bore is needed).
        solve     : re-solve the mate network immediately (default) and report status.

        Returns the mate id (drive it with simulate_motion for kinematics), its individual
        status (even beyond the worst five), total/omitted counts, the chosen reference
        directions and signed branch, `moved_components` (the ids whose pose this solve
        changed) with their world bboxes in `world_bodies_changed` — not every body
        (`list_world_bodies` is the full inventory; body counts are included).
        `source_status` reports the pre-solve fingerprint state (current, stale, or
        not_built); `source_states` also flags live unsaved or missing sources independently.
        `source_problems` lists failed component builds. Source geometry changes
        without a proven match leave a mate unresolved; its source-frame candidates are
        suggestions, not automatic remaps.
        A committed mate with a successful individual status returns ok=true even if
        other mates prevent network convergence. Inspect network_converged and
        blocking_mates, fix the existing network, and do not retry mate creation.
        """
        try:
            d = _doc(doc_id)
            source_status = cb.call("asm_source_status", {"doc_id": d})
            if source_status.get("ok") is False:
                return _dumps(source_status)
            comps = {c.get("id"): c for c in _asm_components(d)}
            if not comps:
                return _dumps({"ok": False, "error": "assembly has no components — "
                               "asm_add_component first (or is this a part doc?)"})
            extra_raw = coerce_json_or_repr(extra) if extra is not None else []
            if not isinstance(extra_raw, list):
                raise ValueError("asm_mate: extra must be a list of operands, e.g. "
                                 "[{'component': 'k3', 'query': {'of':'face','side':'top'}}].")
            if align not in ("aligned", "anti"):
                raise ValueError(f"asm_mate: align must be 'aligned' or 'anti', got {align!r}.")
            kind = _check_mate_request(mate_type, value, 2 + len(extra_raw))
            # LIVE-DIALECT (run-97813015264134 pattern): a model can send `a`/`b` as a
            # stringified dict ("{'component': 'k1', ...}"), a bare component-id string, a
            # one-element list, or the {"kind":"component","id":...} selector shape other ref
            # slots use — _normalize_mate_operand reshapes every equivalent form; only a
            # genuinely unresolvable value raises.
            refs = []
            operands = [("a", a), ("b", b)] + [(f"extra[{i}]", raw)
                                               for i, raw in enumerate(extra_raw)]
            for tag, raw in operands:
                try:
                    ref = _normalize_mate_operand(tag, raw)
                except ValueError as e:
                    return _dumps({"ok": False, "error": str(e)})
                resolved = resolve_mate_ref(d, comps, tag, ref)
                if "error" in resolved:
                    return _dumps({"ok": False, **resolved})
                refs.append(resolved["ref"])
            mate: dict = {"type": kind, "align": align, "suppressed": False,
                          "refs": refs}
            if value is not None:
                mate["value"] = dict(value) if isinstance(value, dict) else {"value": float(value)}
                if (kind in ("angle", "hinge") and isinstance(value, dict)
                        and "axis_point" in value
                        and ("axis" in value or "reference_axis" in value)):
                    import numpy as np
                    transform = np.asarray(comps[refs[0]["component"]]["transform"],
                                           dtype=float).reshape(4, 4)
                    inverse = np.linalg.inv(transform)
                    point = np.asarray(value["axis_point"], dtype=float)
                    axis_key = "axis" if "axis" in value else "reference_axis"
                    axis = np.asarray(value[axis_key], dtype=float)
                    if (point.shape != (3,) or axis.shape != (3,)
                            or not np.isfinite(point).all() or not np.isfinite(axis).all()
                            or np.linalg.norm(axis) < 1e-12):
                        raise ValueError(f"{kind} hinge line needs finite 3-vectors and a nonzero axis")
                    mate["value"]["axis_point"] = (inverse[:3, :3] @ point + inverse[:3, 3]).tolist()
                    mate["value"][axis_key] = (inverse[:3, :3] @ axis).tolist()
            r = cb.call("doc_apply_ops", {"doc_id": d,
                                          "with_tess": False,
                                          "ops": [{"op": "asm_add_mate", "mate": mate}]})
            mate_id = (r.get("id_map") or {}).get("$0")
            if r.get("ok") is False or not mate_id:
                return _dumps({"ok": False, "error": r.get("error") or
                               "mate creation did not return a committed mate id"})
            # Both mated components now owe their pose to the solver, not to a raw transform.
            for ref in refs:
                note_placement_source(d, ref.get("component"), "mate")
            out: dict = {"ok": True, "doc_id": d, "mate": mate_id, "mate_id": mate_id,
                         "mate_status": None, "type": kind,
                         "placement_source": {ref["component"]: placement_source_of(
                             d, ref["component"]) for ref in refs}}
            if solve:
                sr = cb.call("doc_solve_mates", {"doc_id": d})
                rb = sr.get("rebuild") or {}
                out["solve_status"] = rb.get("doc_status")
                rep = _solve_report(rb, mate_id)
                out["converged"] = rep["converged"]
                out["network_converged"] = rep["converged"]
                out["blocking_mates"] = rep["blocking_mates"]
                out["mate_residuals"] = rep["worst"]
                out["mate_residual_total"] = rep["mate_total"]
                out["mate_residual_omitted"] = rep["mate_omitted"]
                new_status = next((m for m in rep["worst"] if m["id"] == mate_id), None)
                out["new_mate_status"] = new_status
                out["mate_status"] = new_status
                out["mate_branch"] = (
                    _mate_branch(mate, sr.get("tree") or {}, rep["converged"])
                    if new_status and new_status.get("status") != "unresolved"
                    else {"available": False})
                out["dof"] = rb.get("dof")
                if "residual_dof" in rb:
                    out["residual_dof"] = rb["residual_dof"]
                if not rep["converged"]:
                    out["solve_error"] = rep["message"]
                    out["worst_mate"] = rep["worst_mate"]
                    out["note"] = "Mate created — do not retry creation; fix or suppress the blocking mates and re-solve."
                    if new_status and new_status.get("status") in ("unresolved", "conflicting", "unsatisfied"):
                        out["ok"] = False
                        out["reason"] = ("mate_unresolved" if new_status["status"] == "unresolved"
                                         else "mate_" + new_status["status"])
                bodies = [b for b in (cb.call("asm_world_bodies", {"doc_id": d}) or {}).get(
                    "bodies") or [] if isinstance(b, dict)]
                after = [c for c in ((sr.get("tree") or {}).get("components") or [])
                         if isinstance(c, dict)]
                if after:
                    moved = _moved_components(comps, after)
                    moved_ids = set(moved)
                    changed = [b for b in bodies
                               if str(b.get("component") or "").split("#", 1)[0] in moved_ids]
                else:
                    # No solved tree came back, so which poses changed is unknown: report
                    # every body rather than claim none moved.
                    moved, changed = None, bodies
                out["moved_components"] = moved
                out["world_bodies_changed"] = changed
                out["world_bodies_unchanged"] = len(bodies) - len(changed)
                out.update(_body_counts(bodies))
                out["source_status"] = {key: source_status.get(key)
                                        for key in ("status", "matches")}
                states = source_states(d, after or list(comps.values()), source_status)
                if states:
                    out["source_states"] = states[:16]
                    out["source_states_omitted"] = max(0, len(states) - 16)
                if rep["source_problems"]:
                    out["source_problems"] = rep["source_problems"]
            else:
                # Applying the operation itself may rebuild and move components even when
                # the extra explicit solve is disabled. Report the actual post-apply poses.
                bodies = [b for b in (cb.call("asm_world_bodies", {"doc_id": d}) or {}).get(
                    "bodies") or [] if isinstance(b, dict)]
                after = [c for c in ((r.get("tree") or {}).get("components") or [])
                         if isinstance(c, dict)]
                moved = _moved_components(comps, after) if after else None
                moved_ids = set(moved) if moved is not None else None
                changed = ([b for b in bodies
                            if str(b.get("component") or "").split("#", 1)[0] in moved_ids]
                           if moved_ids is not None else bodies)
                out.update(_body_counts(bodies))
                out["moved_components"] = moved
                out["world_bodies_changed"] = changed
                out["world_bodies_unchanged"] = len(bodies) - len(changed)
                out["source_status"] = {key: source_status.get(key)
                                        for key in ("status", "matches")}
                states = source_states(d, after or list(comps.values()), source_status)
                if states:
                    out["source_states"] = states[:16]
                    out["source_states_omitted"] = max(0, len(states) - 16)
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

        `source_status` captures the live source fingerprint match before the solve
        refreshes it: current, stale, or not_built; its `sources` lists only changed or
        open-and-unsaved component sources (`sources_total` counts all). `source_states` identifies changed,
        missing, or independently live-unsaved sources; `source_problems` lists failures.
        """
        try:
            d = _doc(doc_id)
            source_status = cb.call("asm_source_status", {"doc_id": d})
            if source_status.get("ok") is False:
                return _dumps(source_status)
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
                   "mate_residuals": rep["worst"], "mate_residual_total": rep["mate_total"],
                   "mate_residual_omitted": rep["mate_omitted"]}
            if "residual_dof" in rb:
                out["residual_dof"] = rb["residual_dof"]
            if rep["source_problems"]:
                out["source_problems"] = rep["source_problems"]
            # Every component's current+cached fingerprints made this reply ~70 KB on a large
            # assembly; only rows with something to act on are listed (the count stays).
            rows = [r for r in (source_status.get("sources") or []) if isinstance(r, dict)]
            flagged = [r for r in rows if r.get("changed") is True
                       or (r.get("open") and r.get("dirty"))]
            out["source_status"] = {**source_status, "sources": flagged,
                                    "sources_total": len(rows)}
            states = source_states(d, _asm_components(d), source_status)
            if states:
                out["source_states"] = states
            if not rep["converged"]:
                out["error"] = rep["message"]
                out["worst_mate"] = rep["worst_mate"]
                if rep["reason"]:
                    out["reason"] = rep["reason"]
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # Register the shared implementations used by motion and animation composition.
    mcp.tool()(simulate_motion)
    mcp.tool()(motion_fea_handoff)



    @mcp.tool()
    def list_world_bodies(explode: bool = False, doc_id: Optional[str] = None) -> str:
        """List a doc's bodies with their WORLD-placed bounding boxes.

        Works on an ASSEMBLY doc (each placed component after mates are solved) AND on a
        multi-body PART — e.g. one assembled via import_part + transform (where the bodies are
        already placed absolutely). Returns each body {name, component, bbox:{min,max,size},
        placement_source}; use it to see where every part sits in the shared frame, or pair
        with check_interference to validate fit. explode=True applies the exploded-view offsets
        on an assembly (ignored on a plain multi-body part).
        `body_count` and `logical_member_count` count these grouped entries. A member
        may contain multiple physical solids, identified by `source_ordinals`;
        `physical_solid_count` sums them, or is null if that census is unavailable.

        `placement_source` is "mate" (solver-derived pose), "transform" (a pose YOU asserted
        via asm_add_component at/rotate — verify it with check_interference + measure_between),
        "origin" (inserted at identity) or null (no provenance recorded in the document). Any
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
            out.update(_body_counts(bodies))
            if r.get("multibody_part"):
                out["multibody_part"] = True
            if warnings:
                out["placement_warnings"] = warnings
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)
