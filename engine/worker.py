"""Newline-delimited JSON-RPC worker, spawned by the native host over stdio.

Protocol (one JSON object per line):
  request : {"id": int, "method": str, "params": {...}}
  response: {"id": int, "ok": true, "result": {...}}
            {"id": int, "ok": false, "error": str}

stdout carries ONLY protocol JSON. All diagnostics go to stderr, and the model's own
captured stdout is returned inside the result — so the channel stays clean.

Methods:
  ping                              -> {"pong": true, "build123d": <version>}
  run_model  {code, name?}          -> build a solid OR a multi-part assembly; STEP/STL +
                                       mass props. If the script defines `parts`/`assembly`
                                       the result carries per-part exports + a fit report
                                       (pairwise interference / clearance); single-`result`
                                       scripts behave exactly as before.
  agent_design {request, name?, session_id?, reset?, max_iters?}
                                    -> run one turn of the agent loop against a persistent
                                       session (needs a provider). Emits live "event"
                                       notification lines per iteration before the result.
  shutdown                          -> graceful exit

Streaming: long-running methods receive an ``emit`` callback. Each call writes a
notification line ``{"id": <reqid>, "event": <kind>, "data": {...}}`` to stdout BEFORE the
final ``{"id", "ok", "result"}`` response, so a UI can show progress as it happens.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from engine import paths
from engine.kernel import run_code_multi

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
log = logging.getLogger("ai-cad.worker")

# Live conversation state keyed by session id, so follow-up requests refine the existing
# part instead of restarting. Held in-process for the life of the worker.
_SESSIONS: dict = {}

EmitFn = Callable[[dict], None]


def _ping(_params: dict, _emit_event: EmitFn) -> dict:
    try:
        import build123d

        ver = getattr(build123d, "__version__", "?")
    except Exception:  # noqa: BLE001
        ver = None
    return {"pong": True, "build123d": ver, "protocol": 2}


def _run_model(params: dict, _emit_event: EmitFn) -> dict:
    code = params["code"]
    name = params.get("name", "model")
    out_dir = paths.model_dir(name)
    # run_code_multi auto-detects parts/assembly; a single-`result` script returns the same
    # geometry/props as before (is_assembly=False, one entry in parts, fit=None).
    res = run_code_multi(code, out_dir, name)
    return asdict(res)


def _agent_design(params: dict, emit_event: EmitFn) -> dict:
    from agent import config, loop

    request = params["request"]
    name = params.get("name", "agent_model")
    session_id = params.get("session_id") or name
    cfg = config.load_config()
    # Iteration budget: explicit param > config.json "max_iters" > loop's generous default.
    # The loop normally converges (stops the moment a render matches) well before the cap.
    mi = params.get("max_iters")
    if mi is None:
        mi = cfg.get("max_iters")
    max_iters = int(mi) if mi else None
    # Plan mode: explicit param > config.json "plan_mode" > ON. (Only the first turn of a
    # session actually plans; follow-up turns refine against the stored plan.)
    plan = params.get("plan")
    if plan is None:
        plan = cfg.get("plan_mode", True)

    if params.get("reset"):
        _SESSIONS.pop(session_id, None)
    session = _SESSIONS.get(session_id)
    if session is None or session.name != name:
        session = loop.Session(name=name)
        _SESSIONS[session_id] = session

    provider = config.get_provider()
    run = loop.run_turn(session, request, provider,
                        on_event=emit_event, max_iters=max_iters,
                        plan_first=bool(plan))
    return asdict(run)


def _get_prompts(_params: dict, _emit_event: EmitFn) -> dict:
    """The built-in prompts (+ the active override), so the GUI's Settings editor can show
    and prefill them without duplicating the text in C++."""
    from agent import config, loop

    return {"system_prompt": loop.SYSTEM_PROMPT,
            "plan_prompt": loop.PLAN_PROMPT,
            "override": config.load_config().get("system_prompt", "")}


# =========================================================================== doc_* RPC (C11)
#
# Document sessions, the op vocabulary, tessellation on rebuild, pick resolution, and minimal
# .step+ save/open. New methods use the STRUCTURED error envelope {code, message, detail}
# (doc_ops.md §5); legacy methods keep their plain-string `error` (worker_rpc.md §2).


def _doc_emit(emit_event: EmitFn):
    """Adapt a session/eval emit-callback (dict) onto the worker's event sink."""
    def _e(data: dict) -> None:
        emit_event(data)
    return _e


def _doc_new(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.new_session(kind=params.get("kind", "part"), name=params.get("name"))
    resp = docsess.rebuild(s)
    resp["doc_id"] = s.doc_id
    return resp


def _doc_open(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import stepplus, session as docsess
    path = params["path"]
    # Route by extension: AI-CAD's native ".step+" zip goes through stepplus.open; a plain neutral
    # geometry file (.step/.stp/.iges/.igs/.brep/.stl/.3mf — the _EXT_TO_FORMAT set) is IMPORTED as
    # a fresh part instead (stepplus.open only understands the .step+ container, so opening a plain
    # STEP used to fail). The import path returns the same doc-state shape (doc_id/tree/revision).
    from engine.doc.formats import _EXT_TO_FORMAT, _handler_import_file
    if Path(path).suffix.lower() in _EXT_TO_FORMAT:
        resp = _handler_import_file({"path": path}, _emit)
        resp.setdefault("warnings", [])
        resp.setdefault("drawings", [])
        return resp
    s, warnings = stepplus.open(path)
    resp = docsess.rebuild(s)
    resp["doc_id"] = s.doc_id
    resp["warnings"] = warnings
    # W12+ — restore any embedded drawings as fresh drawing sessions (source re-pointed to this
    # reopened doc) and surface them so the host can re-open their Drawing tabs. Additive +
    # contained: a file with no drawings returns [], a malformed drawing json is skipped.
    try:
        resp["drawings"] = stepplus.restore_drawings(s)
    except Exception:  # noqa: BLE001 — drawing restore must never break the part open
        resp["drawings"] = []
    return resp


def _doc_save(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import stepplus, session as docsess
    s = docsess.get_session(params["doc_id"])
    path = params.get("path") or s.path
    if not path:
        from engine.doc.ops import OpError
        raise OpError("io_error", "doc_save: no path given and document has no current path",
                      {"reason": "missing path"})
    out = stepplus.save(s, path)
    return {"path": out}


def _doc_close(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    closed = docsess.close_session(params["doc_id"])
    return {"closed": closed}


def _doc_list(_params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    return {"docs": docsess.list_sessions()}


def _doc_apply_ops(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.apply_ops(s, params.get("base_revision"), params.get("ops", []),
                             emit=_doc_emit(emit), coalesce=bool(params.get("coalesce", False)))


def _doc_undo(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    return docsess.undo(docsess.get_session(params["doc_id"]), emit=_doc_emit(emit))


def _doc_redo(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    return docsess.redo(docsess.get_session(params["doc_id"]), emit=_doc_emit(emit))


def _doc_tree(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    return docsess.tree(docsess.get_session(params["doc_id"]))


def _doc_rebuild(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    return docsess.rebuild(docsess.get_session(params["doc_id"]), emit=_doc_emit(emit))


def _doc_resolve_pick(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.resolve_pick(
        s, params.get("revision"),
        int(params["body_id"]), params["kind"], int(params["topo_id"]))


def _doc_checkpoint(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.checkpoint(s)


def _doc_revert(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    ckpt = params.get("checkpoint")
    if not isinstance(ckpt, str) or not ckpt:
        from engine.doc.ops import OpError
        raise OpError("invalid_op", "doc_revert requires a 'checkpoint' id",
                      {"reason": "missing checkpoint"})
    return docsess.revert(s, ckpt, emit=_doc_emit(emit))


# =========================================================================== C20 assembly methods
#
# doc_solve_mates  {doc_id, drag?: {component, target:[x,y,z]}}  -> ops-response (re-solve mates,
#   persist solved transforms, bump revision). A drag soft-pulls one component's translation toward
#   target along its free DOF (constrained drag); without drag it just re-solves.
# doc_set_explode_state {doc_id, active: bool, factor: 0..1} -> ops-response (re-tessellate with the
#   explode offsets applied; view-only, no revision bump).


def _doc_refresh_derived(params: dict, emit: EmitFn) -> dict:
    # Re-pull cross-doc derived-paste features from their live source document(s).
    # id omitted -> refresh every derived group in the doc; id given -> that group only.
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.refresh_derived(s, id=params.get("id"), emit=_doc_emit(emit))


def _doc_solve_mates(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.solve_mates(s, drag=params.get("drag"), emit=_doc_emit(emit))


def _doc_set_explode_state(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.set_explode_state(s, bool(params.get("active", False)),
                                     float(params.get("factor", 0.0)), emit=_doc_emit(emit))


# ============================================================== C19 doc-side sim bridge handlers
#
# Produce sim INPUTS from the live doc session (the sim worker is a separate process that only
# sees files + study JSON). doc_export_step -> STEP of the last-good bodies; doc_face_points ->
# a sampled surface point cloud for a face selector (fea_static FaceSet). See engine/sim/bridge.py.


def _doc_export_step(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    from engine.sim import bridge
    s = docsess.get_session(params["doc_id"])
    return {"path": bridge.export_step(s, params["path"])}


def _doc_face_points(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    from engine.sim import bridge
    s = docsess.get_session(params["doc_id"])
    return bridge.face_points(s, params["selector"], int(params.get("max_n", 400)))


# =========================================================================== C18 sketch methods
#
# Sketch-mode interaction (worker_rpc.md §2 sketch_*). Thin adapters onto engine.doc.session, which
# warm-starts engine.doc.sketch_solver (C10). sketch_drag is transient (no revision bump);
# sketch_drag_commit/sketch_exit return the ops-response (rebuild). Structured error envelope.


def _sketch_enter(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_enter(s, params["sketch"])


def _sketch_solve(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_solve(s, params["sketch"])


def _sketch_probe(params: dict, _emit: EmitFn) -> dict:
    # C50f over-define pre-flight: dry-run an add_constraint/set_dim/delete_constraint on a deep copy
    # of the active sketch and report would_over_define. READ-ONLY — never mutates the doc, the
    # session sketch state, the undo stack, or the warm-start state.
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_probe(s, params["sketch"], params.get("action", {}))


def _sketch_drag(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    # `targets` (multi-point profile / group drag) is optional; single-point drags still pass
    # handle + xy. Either path is honored by session.sketch_drag.
    return docsess.sketch_drag(s, params["sketch"], params.get("handle"),
                               params.get("xy"), params.get("targets"))


def _sketch_drag_commit(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_drag_commit(s, params["sketch"], emit=_doc_emit(emit))


def _sketch_exit(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_exit(s, params["sketch"], emit=_doc_emit(emit))


def _doc_sketch_overview(params: dict, _emit: EmitFn) -> dict:
    # C40: read-only sketch snapshot (plane_frame + solved entities) for ghost rendering of
    # inactive sketches in the viewport. Never touches session.active_sketch.
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_overview(s, params["sketch"])


def _doc_sketch_regions(params: dict, _emit: EmitFn) -> dict:
    # C51: read-only even-odd region enumeration for the feature-PM viewport overlay
    # (hover-highlight + click-to-toggle profile picking). Never mutates the doc.
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_regions(s, params["sketch"])


def _sketch_project_ref(params: dict, _emit: EmitFn) -> dict:
    # C50: read-only projection of a datum plane / planar face / model edge into a sketch's
    # (u,v) plane. {doc_id, sketch, ref} -> {entity, source}; never mutates the doc, never
    # bumps the revision, never rebuilds meshes.
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.sketch_project_ref(s, params["sketch"], params.get("ref"))


# =========================================================================== CONFIG config_* RPC
#
# Named configurations (param overrides + feature/component suppression). All config_* methods take
# {doc_id, ...}. config_list is read-only; the rest mutate the doc (one undoable step) and return
# the ops-response. config_activate / editing-the-active-config rebuild; editing an inactive config
# is a metadata-only write that reuses the last build. Structured error envelope.


def _weld_cut_list(params: dict, _emit: EmitFn) -> dict:
    # Weldments cut list: aggregate the active part doc's weld_member structural members into a
    # grouped cut-list table (read-only; ensures-built then reads the _weld_members side-channel).
    # A doc with no weld_member features -> empty rows (not an error).
    from engine.doc import session as docsess
    from engine.doc import cutlist
    return cutlist.cut_list_for_session(docsess.get_session(params["doc_id"]))


def _config_list(params: dict, _emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    return docsess.config_list(docsess.get_session(params["doc_id"]))


def _config_add(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.config_add(s, params["name"], params.get("copy_from"),
                              emit=_doc_emit(emit))


def _config_delete(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.config_delete(s, params["name"], emit=_doc_emit(emit))


def _config_activate(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.config_activate(s, params["name"], emit=_doc_emit(emit))


def _config_set_override(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    # value == null (and no clear flag) means CLEAR the override (use the base param value).
    value = params.get("value")
    clear = bool(params.get("clear", False)) or value is None
    return docsess.config_set_override(s, params["name"], params["feature_id"],
                                       params["param"], value, clear=clear,
                                       emit=_doc_emit(emit))


def _config_set_suppression(params: dict, emit: EmitFn) -> dict:
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    return docsess.config_set_suppression(s, params["name"], params["feature_id"],
                                          bool(params.get("suppressed", False)),
                                          emit=_doc_emit(emit))


# =========================================================================== eval_* RPC (C12)
#
# Per reports.md §C12: nine handlers over the active document's bodies. Field handlers tessellate
# the active body, run the field fn, and write_aicm([tess], extra_chunks=[chunk]) to a temp path,
# returning {aicm_path}. mm / degrees / grams throughout.


def _eval_active_shape(doc_id: str, body_id: int | None = None):
    """Return (session, BodyOut, result) for the active (or selected) solid body.

    For an assembly, ``body_id`` selects a component (1-based ordinal) and the returned BodyOut
    carries that component's LOCAL shape (mass/CoG are transform-invariant, so local is fine)."""
    from engine.doc import session as docsess
    s = docsess.get_session(doc_id)
    if s.is_assembly:
        from engine.doc.compiler import BodyOut
        comps = docsess.asm_component_bodies(s)
        if not comps:
            from engine.doc.ops import OpError
            raise OpError("invalid_op", "assembly has no components to evaluate",
                          {"reason": "no components"})
        if body_id is not None:
            if body_id < 1 or body_id > len(comps):
                from engine.doc.ops import OpError
                raise OpError("invalid_op", f"body_id {body_id} out of range",
                              {"reason": "bad body_id"})
            c = comps[body_id - 1]
        else:
            c = comps[0]
        bo = BodyOut(body_id=str(body_id or 1), name=c["name"], shape=c["shape"], color=None)
        return s, bo, s.last_result or docsess._ensure_built(s)
    result = docsess._ensure_built(s)
    bodies = list(result.bodies)
    if not bodies:
        from engine.doc.ops import OpError
        raise OpError("invalid_op", "document has no bodies to evaluate",
                      {"reason": "no bodies"})
    if body_id is not None:
        if body_id < 1 or body_id > len(bodies):
            from engine.doc.ops import OpError
            raise OpError("invalid_op", f"body_id {body_id} out of range", {"reason": "bad body_id"})
        return s, bodies[body_id - 1], result
    return s, bodies[-1], result


def _eval_write_field_aicm(doc_id: str, body, chunk_fn) -> str:
    """Tessellate *body*, run chunk_fn(tess, shape)->(...,chunk_bytes), write AICM to temp."""
    import tempfile
    from engine.doc import tessellate as tess
    bt = tess.tessellate_body(body.shape, body_id=1, name=body.name or "Body1",
                              color=tuple(body.color) if body.color else (0.71, 0.76, 0.82))
    chunk = chunk_fn(bt, body.shape)
    with tempfile.NamedTemporaryFile(suffix=".aicm", delete=False) as tf:
        tmp = tf.name
    tess.write_aicm_file(tmp, [bt], extra_chunks=[chunk])
    return tmp


def _eval_mass(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    _, body, _r = _eval_active_shape(params["doc_id"], params.get("body"))
    density = params.get("density_g_cm3", 7.85)
    return evaluate.mass_props(body.shape, density)


def _eval_measure(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    result = docsess._ensure_built(s)
    bodies = list(result.bodies)
    infos = []
    for ref in params.get("refs", []):
        infos.append(_resolve_eval_ref(ref, result, bodies))
    return evaluate.measure(infos)


def _resolve_eval_ref(ref: dict, result, bodies) -> dict:
    """Resolve a selector ref to a {shape, kind} dict for evaluate.measure (best-effort)."""
    from engine.doc import selectors as sel
    kind = ref.get("kind")
    # Resolve against the last body's topology (M5 multi-body refinement is a later card).
    if not bodies:
        from engine.doc.ops import OpError
        raise OpError("invalid_op", "no bodies for measure", {"reason": "no bodies"})
    body = bodies[-1]
    raw = body.shape.wrapped
    diag = 100.0
    try:
        bb = body.shape.bounding_box()
        import math
        diag = max(math.sqrt((bb.max.X - bb.min.X) ** 2 + (bb.max.Y - bb.min.Y) ** 2
                             + (bb.max.Z - bb.min.Z) ** 2), 1e-3)
    except Exception:  # noqa: BLE001
        pass
    if kind == "face":
        infos = sel.describe_faces(raw)
        elems = body.shape.faces()
    elif kind == "edge":
        infos = sel.describe_edges(raw)
        elems = body.shape.edges()
    elif kind == "vertex":
        infos = sel.describe_vertices(raw)
        elems = body.shape.vertices()
    else:
        from engine.doc.ops import OpError
        raise OpError("invalid_op", f"measure ref kind '{kind}' unsupported",
                      {"reason": "bad kind"})
    rr = sel.resolve(ref, infos, diag)
    if rr.index is None:
        from engine.doc.ops import OpError
        raise OpError("invalid_op", f"measure ref dangling: {rr.dangling_reason}",
                      {"reason": rr.dangling_reason})
    return {"shape": elems[rr.index], "kind": kind}


def _eval_section_props(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    result = docsess._ensure_built(s)
    body = list(result.bodies)[-1]
    plane = _resolve_eval_plane(params.get("plane"), s)
    return evaluate.section_props(body.shape, plane)


def _resolve_eval_plane(plane_ref, session):
    """Resolve a plane ref (datum id / datum selector) to a build123d Plane for section/symmetry."""
    from engine.doc.features.datum import reserved_datums
    from engine.doc.ops import OpError
    fid = None
    if isinstance(plane_ref, str):
        fid = plane_ref
    elif isinstance(plane_ref, dict) and plane_ref.get("kind") == "datum":
        fid = plane_ref.get("feature")
    if fid is None:
        raise OpError("invalid_op", "plane must be a datum id or datum selector",
                      {"reason": "bad plane"})
    d = reserved_datums().get(fid)
    if d is None or d.kind != "plane":
        raise OpError("invalid_op", f"plane '{fid}' is not a reserved planar datum",
                      {"reason": "bad plane"})
    return d.obj


def _eval_interference(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    from engine.doc import session as docsess
    s = docsess.get_session(params["doc_id"])
    if s.is_assembly:
        # World-placed component bodies (clearance/interference between distinct components).
        members = docsess.asm_world_bodies(s, explode=False)
        return evaluate.interference(members)
    result = docsess._ensure_built(s)
    members = [(bo.name or bo.body_id, bo.shape) for bo in result.bodies]
    return evaluate.interference(members)


def _eval_check(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    _, body, _r = _eval_active_shape(params["doc_id"], params.get("body"))
    return evaluate.check(body.shape)


def _eval_symmetry(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    s, body, _r = _eval_active_shape(params["doc_id"], params.get("body"))
    plane = _resolve_eval_plane(params.get("plane"), s)
    return evaluate.symmetry(body.shape, plane)


def _eval_draft(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    _, body, _r = _eval_active_shape(params["doc_id"], params.get("body"))
    pull = params.get("pull", [0.0, 0.0, 1.0])
    angle = params.get("angle_deg", 0.0)
    path = _eval_write_field_aicm(
        params["doc_id"], body,
        lambda bt, sh: evaluate.draft_field(bt, sh, pull, angle)[3])
    return {"aicm_path": path}


def _eval_thickness(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    _, body, _r = _eval_active_shape(params["doc_id"], params.get("body"))
    path = _eval_write_field_aicm(
        params["doc_id"], body,
        lambda bt, sh: evaluate.thickness_field(bt, sh)[3])
    return {"aicm_path": path}


def _eval_curvature(params: dict, _emit: EmitFn) -> dict:
    import engine.doc.evaluate as evaluate
    _, body, _r = _eval_active_shape(params["doc_id"], params.get("body"))
    kind = params.get("kind", "mean")
    path = _eval_write_field_aicm(
        params["doc_id"], body,
        lambda bt, sh: evaluate.curvature_field(bt, sh, kind)[3])
    return {"aicm_path": path}


# =========================================================================== C22a orchestrator (M9)
#
# The deterministic AI orchestrator (agent/orchestrator). agent_run_v2 runs a design against an
# OPEN doc session, streaming the events.md §3 hierarchy (run/stage/agent_call/agent_output/
# stage_result/run_finished) via the per-request emit fn. agent_cancel flags a run for best-effort
# cancel (checked at stage boundaries). get_prompts_v2 exposes the per-role editable prompts. The
# legacy agent_design path (above) is untouched.

# Active orchestrator runs: run_id -> RunControl (cooperative cancel flag).
_RUNS: dict = {}


def _agent_run_v2(params: dict, emit_event: EmitFn) -> dict:
    from agent import config
    from agent.orchestrator import run as orun
    from agent.orchestrator.machine import RunControl
    from engine.doc import session as docsess
    from dataclasses import asdict

    request = params["request"]
    s = docsess.get_session(params["doc_id"])
    provider = config.get_provider()
    options = dict(params.get("options") or {})
    control = RunControl()
    run_id = options.get("run_id")
    if run_id:
        _RUNS[run_id] = control
    options["control"] = control

    seen_ids: set = set()

    def _register_then_emit(ev: dict) -> None:
        # The first event (run_started) carries the generated run_id; register the control under it
        # so agent_cancel can find a run that didn't pass an explicit id.
        rid = ev.get("run_id")
        if rid:
            seen_ids.add(rid)
            if rid not in _RUNS:
                _RUNS[rid] = control
        emit_event(ev)

    # C35: route to the MICRO-ORCHESTRATION profile for small-context local models (decompose ->
    # per-step execute + python verify). Falls back to the full v2 machine otherwise. The RPC
    # options may explicitly request a workflow/profile; this keeps the documented worker surface
    # aligned with the actual dispatcher.
    from agent.orchestrator import micro
    agent_cfg = config.load_config()
    workflow = str(options.get("workflow") or options.get("profile") or "").strip().lower()
    if workflow in ("micro", "local", "small-context", "small_context", "tiny"):
        agent_cfg["micro_mode"] = True
    elif workflow in ("full", "standard", "machine"):
        agent_cfg["micro_mode"] = False
    elif workflow not in ("", "auto"):
        raise ValueError("agent_run_v2 options.workflow must be auto, micro, or full")
    use_micro = micro.should_engage(provider, agent_cfg)
    options["workflow"] = "micro" if use_micro else "full"

    try:
        if use_micro:
            result = micro.run_micro_design(request, s, provider,
                                            emit=_register_then_emit, options=options)
        else:
            result = orun.run_design(request, s, provider,
                                     emit=_register_then_emit, options=options)
        seen_ids.add(result.run_id)
        return asdict(result)
    finally:
        for rid in seen_ids:
            _RUNS.pop(rid, None)
        if run_id:
            _RUNS.pop(run_id, None)


def _agent_cancel(params: dict, _emit_event: EmitFn) -> dict:
    """Best-effort cancel: flag the run; the machine checks the flag at the next stage boundary."""
    run_id = params["run_id"]
    control = _RUNS.get(run_id)
    if control is None:
        return {"cancelled": False, "reason": "unknown or finished run"}
    control.cancelled = True
    return {"cancelled": True}


def _get_prompts_v2(_params: dict, _emit_event: EmitFn) -> dict:
    """Per-role editable prompt records for the GUI prompt editor (diff vs built-in, reset)."""
    from agent.orchestrator import roles
    return {"prompts": roles.get_prompts_v2()}


# =========================================================================== ask mode (Wave D)
#
# agent_ask_v2 {doc_id, question} -> {answer}: answers a QUESTION about the open part WITHOUT
# mutating anything. Thin adapter — the prompt assembly + single provider call live in
# agent/ask.py (testable without the worker subprocess). A missing/empty/unknown doc_id is
# handled gracefully: the assistant answers about general CAD usage instead of erroring.


def _agent_ask_v2(params: dict, _emit_event: EmitFn) -> dict:
    from agent import ask, config

    question = params["question"]
    session = None
    doc_id = params.get("doc_id") or ""
    if doc_id:
        try:
            from engine.doc import session as docsess
            session = docsess.get_session(doc_id)
        except Exception:  # noqa: BLE001 — unknown doc => answer about general usage
            session = None
    provider = config.get_provider()
    return {"answer": ask.answer_question(question, session, provider)}


_METHODS = {
    "ping": _ping,
    "run_model": _run_model,
    "agent_design": _agent_design,
    "get_prompts": _get_prompts,
    # Documents (C11)
    "doc_new": _doc_new,
    "doc_open": _doc_open,
    "doc_save": _doc_save,
    "doc_close": _doc_close,
    "doc_list": _doc_list,
    "doc_apply_ops": _doc_apply_ops,
    "doc_undo": _doc_undo,
    "doc_redo": _doc_redo,
    "doc_tree": _doc_tree,
    "doc_rebuild": _doc_rebuild,
    "doc_resolve_pick": _doc_resolve_pick,
    "doc_checkpoint": _doc_checkpoint,
    "doc_revert": _doc_revert,
    "doc_refresh_derived": _doc_refresh_derived,  # cross-doc derived-paste live relink
    # Assembly (C20)
    "doc_solve_mates": _doc_solve_mates,
    "doc_set_explode_state": _doc_set_explode_state,
    # Sketch mode (C18)
    "sketch_enter": _sketch_enter,
    "sketch_solve": _sketch_solve,
    "sketch_probe": _sketch_probe,  # C50f over-define pre-flight (read-only)
    "sketch_drag": _sketch_drag,
    "sketch_drag_commit": _sketch_drag_commit,
    "sketch_exit": _sketch_exit,
    "doc_sketch_overview": _doc_sketch_overview,  # C40 ghost rendering
    "doc_sketch_regions": _doc_sketch_regions,  # C51 feature-PM region overlay (read-only)
    "sketch_project_ref": _sketch_project_ref,  # C50 project-into-sketch (read-only)
    # Evaluate (C12)
    "eval_mass": _eval_mass,
    "eval_measure": _eval_measure,
    "eval_section_props": _eval_section_props,
    "eval_interference": _eval_interference,
    "eval_check": _eval_check,
    "eval_symmetry": _eval_symmetry,
    "eval_draft": _eval_draft,
    "eval_thickness": _eval_thickness,
    "eval_curvature": _eval_curvature,
    # C19 doc-side sim bridge
    "doc_export_step": _doc_export_step,
    "doc_face_points": _doc_face_points,
}

# C21 formats (import_file / export_file) register with the same (params, emit) convention.
from engine.doc import formats as _formats  # noqa: E402

_METHODS.update(_formats.WORKER_METHODS)

# Instant3D push-pull (drag-a-face-to-edit). Registered via its own WORKER_METHODS dict so the
# literal above stays owned by the worker integrator.
from engine.doc import instant3d as _instant3d  # noqa: E402

_METHODS.update(_instant3d.WORKER_METHODS)

# W12 DRAWINGS — 2D orthographic/iso views of a part/assembly via HLR. Its own WORKER_METHODS dict
# (drawing_new / drawing_add_view / drawing_get / drawing_remove_view / drawing_set_view /
# drawing_close) keeps the literal above frozen.
from engine.doc import drawing as _drawing  # noqa: E402

_METHODS.update(_drawing.WORKER_METHODS)

# C22a orchestrator (M9) — registered via update so the shared _METHODS dict literal stays owned by
# the worker integrator. agent_run_v2/agent_cancel/get_prompts_v2 keep the LEGACY plain-string error
# shape (they are not in _STRUCTURED_ERROR_METHODS), matching agent_design.
_METHODS.update({
    "agent_run_v2": _agent_run_v2,
    "agent_cancel": _agent_cancel,
    "get_prompts_v2": _get_prompts_v2,
})

# Methods that emit the structured error envelope {code, message, detail} on failure.
_STRUCTURED_ERROR_METHODS = {
    "doc_new", "doc_open", "doc_save", "doc_close", "doc_list", "doc_apply_ops",
    "doc_undo", "doc_redo", "doc_tree", "doc_rebuild", "doc_resolve_pick",
    "doc_checkpoint", "doc_revert", "doc_refresh_derived",
    "doc_solve_mates", "doc_set_explode_state",  # C20
    "sketch_enter", "sketch_solve", "sketch_probe", "sketch_drag", "sketch_drag_commit",
    "sketch_exit",
    "doc_sketch_overview",  # C40
    "doc_sketch_regions",  # C51
    "sketch_project_ref",  # C50
    "eval_mass", "eval_measure", "eval_section_props", "eval_interference", "eval_check",
    "eval_symmetry", "eval_draft", "eval_thickness", "eval_curvature",
    "doc_export_step", "doc_face_points",  # C19
    "import_file", "export_file",  # C21
    "doc_push_pull",  # Instant3D push-pull (drag a face)
    # W12 drawings — all use the structured error envelope.
    "drawing_new", "drawing_add_view", "drawing_remove_view", "drawing_set_view",
    "drawing_get", "drawing_close",
    # W13 drawing annotations + BOM.
    "drawing_add_dim", "drawing_remove_annotation", "drawing_bom",
    # W15 section + detail views.
    "drawing_add_section", "drawing_add_detail",
}

# ---- ask mode (Wave D) — registered via update/add so the literals above stay frozen ----
_METHODS.update({"agent_ask_v2": _agent_ask_v2})
_STRUCTURED_ERROR_METHODS.add("agent_ask_v2")

# ---- CONFIG configurations — config_* RPCs (registered via update so the literal stays frozen) --
_METHODS.update({
    "config_list": _config_list,
    "config_add": _config_add,
    "config_delete": _config_delete,
    "config_activate": _config_activate,
    "config_set_override": _config_set_override,
    "config_set_suppression": _config_set_suppression,
})
_STRUCTURED_ERROR_METHODS.update({
    "config_list", "config_add", "config_delete", "config_activate",
    "config_set_override", "config_set_suppression",
})

# ---- WELDMENTS cut list — read-only aggregation of weld_member members (structured error) ----
_METHODS.update({"weld_cut_list": _weld_cut_list})
_STRUCTURED_ERROR_METHODS.add("weld_cut_list")

# ---- 2D EXPORT — drawing sheets + sheet-metal flat patterns to DXF/PDF (structured error) ----
# export_drawing {doc_id(drawing), path, format:"dxf"|"pdf"} and
# export_flat_dxf {doc_id(part), path, format?:"dxf"|"pdf"} register via their own WORKER_METHODS so
# the literal above stays owned by the worker integrator.
from engine.doc import export2d as _export2d  # noqa: E402

_METHODS.update(_export2d.WORKER_METHODS)
_STRUCTURED_ERROR_METHODS.update({"export_drawing", "export_flat_dxf"})

# ---- Supplier/vendor parts cache + import features (structured error) ----
from engine import vendor_parts as _vendor_parts  # noqa: E402

_METHODS.update(_vendor_parts.WORKER_METHODS)
_STRUCTURED_ERROR_METHODS.update(_vendor_parts.WORKER_METHODS.keys())

# === ECAD (Wave C2) — AI-ECAD electrical design methods (schematic/PCB/sim/fab/3d) ===
try:
    from engine.ecad.rpc import METHODS as _ECAD_METHODS, STRUCTURED_ERROR_METHODS as _ECAD_SEM
    _METHODS.update(_ECAD_METHODS)
    _STRUCTURED_ERROR_METHODS |= _ECAD_SEM
except Exception as _ecad_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "engine.ecad.rpc unavailable — ecad_* methods not registered: %s", _ecad_import_err)
# === end ECAD ===


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve() -> None:
    log.info("worker ready (runtime=%s)", paths.RUNTIME)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:  # noqa: BLE001
            _emit({"id": None, "ok": False, "error": f"bad json: {e}"})
            continue
        rid = req.get("id")
        method = req.get("method")
        if method == "shutdown":
            _emit({"id": rid, "ok": True, "result": {"bye": True}})
            break
        fn = _METHODS.get(method)
        if fn is None:
            _emit({"id": rid, "ok": False, "error": f"unknown method: {method}"})
            continue

        # Per-request event sink: tags each progress notification with this request's id.
        def emit_event(data: dict, _rid=rid) -> None:
            _emit({"id": _rid, "event": data.get("kind", "event"), "data": data})

        try:
            result = fn(req.get("params") or {}, emit_event)
            _emit({"id": rid, "ok": True, "result": result})
        except Exception as e:  # noqa: BLE001
            if method in _STRUCTURED_ERROR_METHODS:
                # New methods (doc_*/eval_*) use the structured envelope of doc_ops.md §5.
                from engine.doc.ops import OpError
                if isinstance(e, OpError):
                    _emit({"id": rid, "ok": False, "error": e.to_envelope()})
                else:
                    _emit({"id": rid, "ok": False, "error": {
                        "code": "internal", "message": f"{type(e).__name__}: {e}",
                        "detail": {"traceback": traceback.format_exc()}}})
            else:
                # Legacy methods keep the plain-string error (frozen wire shape).
                # Lead with the concise "Type: message" (callers often show only the first line)
                # then the full traceback for debugging.
                _emit({"id": rid, "ok": False,
                       "error": f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"})


if __name__ == "__main__":
    serve()
