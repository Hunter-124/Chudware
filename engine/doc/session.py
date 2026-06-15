"""Document sessions — live state + rebuild + the ops-response (docs/contracts/doc_ops.md §1).

A :class:`DocSession` holds one open document, its revision counter, bounded undo/redo stacks of
full document-JSON snapshots, a per-session compile cache, and the last :class:`CompileResult`.
:func:`apply_ops` runs the op batch (engine.doc.ops), bumps the revision, rebuilds (compile +
tessellate + write AICM), and returns the ops-response dict the worker hands to the host.

resolve_pick maps the C++ side's ``{body_id, kind, topo_id}`` (topo_id = 1-based TopExp map index,
exactly how the compiler keys face_owners/edge_owners) into a C03 selector: describe the resolved
body's faces/edges/vertices in-scope, pick the entity at ``topo_id - 1`` (describe order == TopExp
order == .faces()/.edges() order), stamp its ``owner_feature`` from the compile result's owner tag,
and emit ``make_selector``.

doc_progress events: a callback is invoked once at rebuild start and once at completion when the
rebuild crosses 300 ms, so the host can show progress on slow recomputes (worker_rpc.md §4).

No GUI assumptions; canonical units throughout.
"""

from __future__ import annotations

import copy
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from engine import paths
from engine.doc.compiler import CompileCache, CompileResult, compile_document
from engine.doc.ops import OpError, apply_ops as _apply_ops_to_doc, is_readonly_batch
from engine.doc import selectors as sel
from engine.doc import tessellate as tess

# Make sure feature compilers are registered (compile_document needs them).
import engine.doc.features  # noqa: F401

EmitFn = Callable[[dict], None]

_UNDO_LIMIT = 100
_PROGRESS_MS = 300.0

# Default body colour (matches the host's grey, doc_ops.md example).
_DEFAULT_COLOR = (0.71, 0.76, 0.82)


def _appearance_material(override):
    """Normalize a stored appearance value (C26 bare ``[r,g,b]`` OR C32-A
    ``{color, metallic, roughness}``) into ``(rgb|None, metallic, roughness)``. A missing override
    returns ``(None, 0.0, 0.5)`` — the dielectric default that renders identically to pre-C32."""
    if override is None:
        return None, 0.0, 0.5
    if isinstance(override, dict):
        col = override.get("color")
        rgb = tuple(float(c) for c in col) if col else None
        return (rgb, float(override.get("metallic", 0.0)), float(override.get("roughness", 0.5)))
    return tuple(float(c) for c in override), 0.0, 0.5


# --------------------------------------------------------------------------- session


@dataclass
class DocSession:
    doc_id: str
    doc: dict
    revision: int = 0
    path: Optional[str] = None
    dirty: bool = False
    undo_stack: list[str] = field(default_factory=list)   # JSON snapshots (doc only)
    redo_stack: list[str] = field(default_factory=list)
    compile_cache: CompileCache = field(default_factory=CompileCache)
    last_result: Optional[CompileResult] = None
    # extra zip entries to preserve on resave (stepplus forward-compat), entry->bytes
    extra_entries: dict[str, bytes] = field(default_factory=dict)
    # C18 — active-sketch interaction state (one sketch edited at a time, SolidWorks-style).
    # Populated by sketch_enter, warm-started across solve/drag, cleared by sketch_exit.
    active_sketch: Optional["_SketchState"] = None
    # C20 — assembly state. asm_result is the last assembly compile (AssemblyResult); asm_warm is
    # the per-component (t,w) warm start carried across re-solves; explode_active/factor drive
    # tessellation-time explode offsets (doc_set_explode_state).
    asm_result: Any = None
    asm_warm: dict = field(default_factory=dict)
    explode_active: bool = False
    explode_factor: float = 0.0
    # C35-overhaul §5 — live PropertyManager-cancel checkpoints: id -> doc-JSON snapshot string.
    # Multiple live checkpoints allowed; all cleared on doc close/new/open.
    checkpoints: dict[str, str] = field(default_factory=dict)
    # CONFIG — name of the last-compiled active configuration (mirrors the compiled
    # CompileResult.active_configuration; "Default" for config-less docs). Surfaced in the
    # ops-response doc_status so the host's config switcher knows which config the geometry reflects.
    active_configuration: str = "Default"

    @property
    def is_assembly(self) -> bool:
        return self.doc.get("schema") == "aicad.asm/1"

    # -- snapshot helpers -------------------------------------------------

    def _snapshot(self) -> str:
        import json
        return json.dumps(self.doc, sort_keys=True, separators=(",", ":"))

    def _push_undo(self) -> None:
        self.undo_stack.append(self._snapshot())
        if len(self.undo_stack) > _UNDO_LIMIT:
            self.undo_stack.pop(0)
        self.redo_stack.clear()


# --------------------------------------------------------------------------- registry


_REGISTRY: dict[str, DocSession] = {}


def get_session(doc_id: str) -> DocSession:
    s = _REGISTRY.get(doc_id)
    if s is None:
        raise OpError("unknown_doc", f"no open document '{doc_id}'", {"doc_id": doc_id})
    return s


def close_session(doc_id: str) -> bool:
    return _REGISTRY.pop(doc_id, None) is not None


def list_sessions() -> list[dict]:
    out: list[dict] = []
    for s in _REGISTRY.values():
        out.append({
            "doc_id": s.doc_id,
            "name": s.doc.get("name", s.doc_id),
            "kind": "assembly" if s.doc.get("schema") == "aicad.asm/1" else "part",
            "path": s.path,
            "revision": s.revision,
            "dirty": s.dirty,
        })
    return out


def _register(session: DocSession) -> None:
    _REGISTRY[session.doc_id] = session


# --------------------------------------------------------------------------- new document


def _blank_part(name: str) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "doc-" + uuid.uuid4().hex[:8],
        "name": name,
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": [],
    }


def _blank_assembly(name: str) -> dict:
    return {
        "schema": "aicad.asm/1",
        "id": "doc-" + uuid.uuid4().hex[:8],
        "name": name,
        "units": {"system": "MMGS"},
        "equations": {"globals": []},
        "components": [],
        "mates": [],
        "features": [],
        "explode": [],
    }


def new_session(kind: str = "part", name: str | None = None) -> DocSession:
    if kind not in ("part", "assembly"):
        raise OpError("invalid_op", f"doc kind must be part|assembly, got '{kind}'",
                      {"reason": "bad kind"})
    doc = _blank_part(name or "Part1") if kind == "part" else _blank_assembly(name or "Assembly1")
    doc_id = uuid.uuid4().hex[:12]
    session = DocSession(doc_id=doc_id, doc=doc, revision=0)
    _register(session)
    return session


def from_document(doc: dict, *, path: str | None = None,
                  extra_entries: dict[str, bytes] | None = None) -> DocSession:
    """Register a session around an already-parsed document (used by stepplus.open)."""
    doc_id = uuid.uuid4().hex[:12]
    session = DocSession(doc_id=doc_id, doc=doc, revision=0, path=path,
                         extra_entries=extra_entries or {})
    _register(session)
    return session


# --------------------------------------------------------------------------- rebuild


def _aicm_path(doc_id: str, revision: int) -> Path:
    d = paths.CACHE / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{doc_id}_r{revision}.aicm"


def _solid_bodies(result: CompileResult) -> list:
    """Real solid bodies only (sketch pseudo-bodies live in tool_bodies, not bodies)."""
    return list(result.bodies)


def _rebuild(session: DocSession, emit: EmitFn | None = None) -> CompileResult:
    """Compile the document and write its AICM. Emits doc_progress when slow (>300 ms)."""
    t0 = time.perf_counter()
    emitted_start = False

    def maybe_progress(stage: str) -> None:
        nonlocal emitted_start
        if emit is None:
            return
        if (time.perf_counter() - t0) * 1000.0 > _PROGRESS_MS:
            if not emitted_start:
                emit({"kind": "doc_progress", "doc_id": session.doc_id, "stage": "rebuild_start"})
                emitted_start = True
            emit({"kind": "doc_progress", "doc_id": session.doc_id, "stage": stage})

    if session.is_assembly:
        _rebuild_assembly(session)
        maybe_progress("assembly_solved")
        # last_result stays a (possibly empty) CompileResult for part-only code paths.
        if session.last_result is None:
            session.last_result = CompileResult()
        return session.last_result

    result = compile_document(session.doc, session.compile_cache)
    maybe_progress("compiled")
    session.last_result = result
    session.active_configuration = result.active_configuration
    return result


def _rebuild_assembly(session: DocSession) -> None:
    """Compile the assembly: solve mates, write solved transforms back into the doc, cache result.

    The active configuration is overlaid onto a NON-MUTATING effective doc first (param overrides +
    feature/component suppression), so an assembly config recompiles the mated model with those
    components dropped / params changed. Solved transforms are written back to the ORIGINAL doc
    (matched by component id) so save still captures the mated pose; suppressed-out components keep
    their stored transform untouched."""
    from engine.doc import assembly as asm
    from engine.doc.compiler import apply_configuration_to_doc
    base_dir = os.path.dirname(session.path) if session.path else None
    active_name, eff_doc = apply_configuration_to_doc(session.doc)
    session.active_configuration = active_name
    ares = asm.compile_assembly(eff_doc, base_dir=base_dir, warm=session.asm_warm or None)
    # Persist solved transforms back into the (original) document (so save captures the mated pose).
    for comp in session.doc.get("components", []):
        if comp["id"] in ares.transforms:
            comp["transform"] = ares.transforms[comp["id"]]
    session.asm_result = ares
    session.asm_warm = ares.warm


def _write_aicm(session: DocSession, result: CompileResult) -> tuple[str, list[dict]]:
    """Tessellate every solid body and write the AICM; return (aicm_path, bodies-list)."""
    if session.is_assembly:
        return _write_aicm_assembly(session)

    body_tesses: list[tess.BodyTess] = []
    bodies_meta: list[dict] = []
    appearances = session.doc.get("appearances", {}) or {}
    for i, bo in enumerate(_solid_bodies(result), start=1):
        # C26 appearance: a per-body color stored in the document under the body's stable key wins
        # over the compiler/default color. The AICM BODY chunk already carries this color and the
        # renderer's PartMesh renders it directly — no new transport needed.
        #
        # Body key = "b{ordinal}" where ordinal is the 1-based AICM body id (the same body_id the
        # host's PickHit carries). It is deterministic by feature order and always present (unlike
        # the face-owner map, which is empty on a pure cache-hit rebuild), so the host can form the
        # set_appearance id directly from a body pick without a resolve round-trip.
        key = f"b{i}"
        rgb, metallic, roughness = _appearance_material(appearances.get(key))
        if rgb is not None:
            color = rgb
        else:
            color = tuple(bo.color) if bo.color else _DEFAULT_COLOR
        bt = tess.tessellate_body(bo.shape, body_id=i, name=bo.name or f"Body{i}", color=color,
                                  metallic=metallic, roughness=roughness)
        body_tesses.append(bt)
        bodies_meta.append({"body_id": i, "name": bo.name or f"Body{i}", "color": list(color),
                            "owner_key": key, "metallic": metallic, "roughness": roughness})

    aicm = _aicm_path(session.doc_id, session.revision)
    tess.write_aicm_file(aicm, body_tesses)
    return str(aicm), bodies_meta


def _write_aicm_assembly(session: DocSession) -> tuple[str, list[dict]]:
    """Tessellate each component (world-placed, optional explode offset) as one AICM body."""
    from engine.doc import assembly as asm
    import numpy as np
    ares = session.asm_result
    body_tesses: list[tess.BodyTess] = []
    bodies_meta: list[dict] = []
    offsets = asm.explode_offsets(session.doc, session.explode_factor) if session.explode_active else {}
    appearances = session.doc.get("appearances", {}) or {}
    for body in (ares.bodies if ares else []):
        off = offsets.get(body.component_id)
        world = asm.placed_world_shape(body, np.asarray(off) if off is not None else None)
        # C26 appearance: assembly bodies are keyed by component id (the stable owner of an
        # assembly body). A stored color wins over the component's own color.
        rgb, metallic, roughness = _appearance_material(appearances.get(body.component_id))
        color = rgb if rgb is not None else tuple(body.color)
        bt = tess.tessellate_body(world, body_id=body.body_id, name=body.name, color=color,
                                  metallic=metallic, roughness=roughness)
        body_tesses.append(bt)
        bodies_meta.append({"body_id": body.body_id, "name": body.name,
                            "color": list(color), "component": body.component_id,
                            "owner_key": body.component_id,
                            "metallic": metallic, "roughness": roughness})
    aicm = _aicm_path(session.doc_id, session.revision)
    tess.write_aicm_file(aicm, body_tesses)
    return str(aicm), bodies_meta


def _tree(session: DocSession) -> dict:
    return copy.deepcopy(session.doc)


def _owner_map(session: DocSession, result: CompileResult) -> dict:
    """Invert the compiler's per-body face/edge owner tags into a per-FEATURE map (C35-overhaul §1).

    The compiler keys ``face_owners``/``edge_owners`` by the per-rebuild string body key ("b1", ...)
    and a 1-based TopExp topo index; :func:`_write_aicm` re-numbers solid bodies to the 1-based AICM
    ordinal (the integer ``body_id`` the host's PickHit and :func:`resolve_pick` use). Here we walk
    those maps and produce, per creating-feature-id::

        {"<fid>": {"bodies": [<body_id>...],
                    "faces": [[body_id, face_id], ...],
                    "edges": [[body_id, edge_id], ...]}}

    where every id is the integer pick id (body ordinal / 1-based topo index). A feature owns a body
    iff it owns at least one of that body's faces (the representative-owner rule used by whole-body
    selectors). Assemblies have no part-feature topology diff, so the map is empty for them.
    """
    out: dict[str, dict] = {}
    if session.is_assembly:
        return out

    def slot(fid: str) -> dict:
        return out.setdefault(fid, {"bodies": [], "faces": [], "edges": []})

    body_face_owners: dict[int, set[str]] = {}
    for ordinal, bo in enumerate(result.bodies, start=1):
        faces = result.face_owners.get(bo.body_id, {}) or {}
        edges = result.edge_owners.get(bo.body_id, {}) or {}
        owners_here: set[str] = set()
        for topo_id, fid in sorted(faces.items()):
            slot(fid)["faces"].append([ordinal, int(topo_id)])
            owners_here.add(fid)
        for topo_id, fid in sorted(edges.items()):
            slot(fid)["edges"].append([ordinal, int(topo_id)])
            owners_here.add(fid)
        body_face_owners[ordinal] = {f for t, f in faces.items()}

    # A feature "owns" a body if it owns any face of it (whole-body attribution).
    for ordinal, fids in body_face_owners.items():
        for fid in sorted(fids):
            slot(fid)["bodies"].append(ordinal)
    for rec in out.values():
        rec["bodies"].sort()
    return out


def _doc_status(session: DocSession, result: CompileResult) -> dict:
    st = session.active_sketch
    mates_status = None
    if session.is_assembly and session.asm_result is not None:
        mates_status = "over" if session.asm_result.over_defined else "ok"
    return {"active_sketch": st.last_overall_status if st else None, "mates": mates_status,
            # CONFIG — which configuration the geometry in this response reflects.
            "active_configuration": session.active_configuration}


def _ops_response(session: DocSession, result: CompileResult,
                  id_map: dict[str, str]) -> dict:
    aicm_path, bodies_meta = _write_aicm(session, result)
    if session.is_assembly and session.asm_result is not None:
        ares = session.asm_result
        # feature_status carries component build status (one row per loaded component).
        feature_status = [
            {"id": cs["id"], "ok": cs["ok"], "ms": cs["ms"], "error": cs["error"],
             "dangling_refs": cs.get("dangling_refs", [])}
            for cs in ares.component_status
        ]
        rebuild = {
            "feature_status": feature_status,
            "doc_status": _doc_status(session, result),
            "components": ares.component_status,
            "mates": ares.mate_status,
            "dof": ares.dof,
            "over_defined": ares.over_defined,
            "dof_summary": ares.dof_summary,
        }
    else:
        feature_status = [
            {"id": s["id"], "ok": s["ok"], "ms": s["ms"], "error": s["error"],
             "dangling_refs": s.get("dangling_refs", []),
             "rolled_back": s.get("rolled_back", False)}
            for s in result.feature_status
        ]
        rebuild = {
            "feature_status": feature_status,
            "doc_status": _doc_status(session, result),
        }
    return {
        "revision": session.revision,
        "id_map": id_map,
        "tree": _tree(session),
        "rebuild": rebuild,
        "tess": {"aicm_path": aicm_path, "bodies": bodies_meta},
        "owner_map": _owner_map(session, result),
    }


# --------------------------------------------------------------------------- mutate


def apply_ops(session: DocSession, base_revision: int | None, ops: list[dict],
              emit: EmitFn | None = None, coalesce: bool = False) -> dict:
    """Apply an op batch, bump revision, rebuild, and return the ops-response (doc_ops.md §1).

    ``base_revision`` (optional optimistic-concurrency guard): if provided and it does not match
    the session's current revision, raises ``stale_revision``.

    ``coalesce`` (undo grouping): when True, do NOT push a new undo snapshot — this batch folds
    into the previous batch's undo step. The host sets it on the 2nd..Nth op of a single user
    action that the engine can only express as several apply_ops calls (e.g. a rectangle = 4 line
    ops whose coincidents need real ids), so one Ctrl+Z reverts the whole action (SolidWorks).
    """
    if base_revision is not None and base_revision != session.revision:
        raise OpError("stale_revision",
                      f"base_revision {base_revision} != current {session.revision}",
                      {"current_revision": session.revision})

    new_doc, id_map = _apply_ops_to_doc(session.doc, ops)  # raises OpError on any failure

    # Read-only fast-path: a batch made up ENTIRELY of read-only ops (e.g. copy_feature) produces no
    # document change — it only surfaces a result through the id_map side-channel (``$bundle``). For
    # such a batch we skip the undo snapshot, the revision bump, and the dirty flag, and we keep the
    # live session.doc untouched (new_doc is a byte-identical deep copy). The op(s) still ran, so the
    # id_map result is returned exactly as before, and the response carries the UNCHANGED revision —
    # so a copy records no phantom undo step / revision and leaves the doc clean. A MIXED batch (any
    # mutating op present) falls through to the normal commit below (snapshot + bump + dirty).
    if is_readonly_batch(ops):
        result = _rebuild(session, emit)
        return _ops_response(session, result, id_map)

    # Commit: snapshot the OLD doc for undo (unless coalescing into the prior step), swap in the
    # new doc, bump revision.
    if not coalesce:
        session._push_undo()
    session.doc = new_doc
    session.revision += 1
    session.dirty = True

    # A feature that profiles a sketch region reads regions from the sketch compiler's
    # context side-channel (ctx.datums['_sketch_regions'], C09). A *cache-hit* sketch does not
    # repopulate that side-channel, so an extrude/revolve added after an unchanged sketch would
    # see "sketch not compiled". When this batch introduces such a consumer, force a clean
    # recompile so the upstream sketch re-registers its regions. (Targeted: ordinary edits keep
    # the cache so the C17 cache-assert still holds.)
    if _batch_consumes_sketch_region(ops):
        session.compile_cache.clear()

    result = _rebuild(session, emit)
    return _ops_response(session, result, id_map)


def _batch_consumes_sketch_region(ops: list[dict]) -> bool:
    """True if any add/edit op carries a region selector in its refs (extrude/revolve/sweep/loft)."""
    def _has_region(obj) -> bool:
        if isinstance(obj, dict):
            if obj.get("kind") == "region":
                return True
            return any(_has_region(v) for v in obj.values())
        if isinstance(obj, list):
            return any(_has_region(v) for v in obj)
        return False

    for op in ops:
        if not isinstance(op, dict):
            continue
        if op.get("op") in ("add_feature", "edit_feature"):
            if _has_region(op.get("feature") or op.get("refs") or {}):
                return True
    return False


def rebuild(session: DocSession, emit: EmitFn | None = None) -> dict:
    """Forced rebuild (no op application, no revision bump)."""
    result = _rebuild(session, emit)
    return _ops_response(session, result, {})


def tree(session: DocSession) -> dict:
    # doc_tree does not rebuild, but it MUST carry the owner_map (C35-overhaul §1). Use the last
    # build if present; otherwise build once so faces/edges have owner tags to expose.
    result = session.last_result if session.last_result is not None else _ensure_built(session)
    return {"revision": session.revision, "tree": _tree(session),
            "owner_map": _owner_map(session, result)}


def undo(session: DocSession, emit: EmitFn | None = None) -> dict:
    if not session.undo_stack:
        raise OpError("invalid_op", "nothing to undo", {"reason": "empty undo stack"})
    import json
    session.redo_stack.append(session._snapshot())
    snap = session.undo_stack.pop()
    session.doc = json.loads(snap)
    session.revision += 1
    session.dirty = True
    session.asm_warm = {}  # re-seed mate solve from the restored doc transforms
    # C50: drop the active sketch's warm solver state so the next sketch_solve COLD-solves the
    # restored doc seeds. Warm-starting from the post-action configuration was non-deterministic —
    # the solver could settle on a different branch, so an undo failed to return the sketch to its
    # exact prior geometry ("calculates relations differently and irreversibly messes up").
    if session.active_sketch is not None:
        session.active_sketch.solve_state = None
    result = _rebuild(session, emit)
    return _ops_response(session, result, {})


def redo(session: DocSession, emit: EmitFn | None = None) -> dict:
    if not session.redo_stack:
        raise OpError("invalid_op", "nothing to redo", {"reason": "empty redo stack"})
    import json
    session.undo_stack.append(session._snapshot())
    if len(session.undo_stack) > _UNDO_LIMIT:
        session.undo_stack.pop(0)
    snap = session.redo_stack.pop()
    session.doc = json.loads(snap)
    session.revision += 1
    session.dirty = True
    session.asm_warm = {}  # re-seed mate solve from the restored doc transforms
    if session.active_sketch is not None:   # C50: cold-solve the restored doc (see undo)
        session.active_sketch.solve_state = None
    result = _rebuild(session, emit)
    return _ops_response(session, result, {})


# =========================================================================== C35-overhaul §5
#
# PropertyManager-cancel checkpoints. doc_checkpoint deep-snapshots the current doc; doc_revert
# restores it, rebuilds, bumps the revision, and pushes ONE undo entry (a single undo after a revert
# returns to the pre-revert state). Checkpoints are per-session and cleared on close/new/open.


def checkpoint(session: DocSession) -> dict:
    """Take a deep snapshot of the current doc and return its checkpoint id (C35-overhaul §5)."""
    cid = "ckpt-" + uuid.uuid4().hex[:10]
    session.checkpoints[cid] = session._snapshot()
    return {"checkpoint": cid}


def revert(session: DocSession, checkpoint_id: str, emit: EmitFn | None = None) -> dict:
    """Restore the doc to a previously-taken checkpoint (C35-overhaul §5).

    Pushes ONE undo entry of the CURRENT (pre-revert) doc, swaps in the snapshot, rebuilds, and
    bumps the revision — so a single undo after a revert returns to the pre-revert state. Reverting
    when the doc already equals the snapshot is cheap (a no-op rebuild) but still returns the full
    ops-response. The checkpoint stays live (re-revertable)."""
    import json
    snap = session.checkpoints.get(checkpoint_id)
    if snap is None:
        raise OpError("invalid_op", f"no such checkpoint '{checkpoint_id}'",
                      {"reason": "unknown checkpoint", "checkpoint": checkpoint_id})
    current = session._snapshot()
    # One undo entry of the pre-revert state (a single undo restores it). Clears redo like any edit.
    session.undo_stack.append(current)
    if len(session.undo_stack) > _UNDO_LIMIT:
        session.undo_stack.pop(0)
    session.redo_stack.clear()
    session.doc = json.loads(snap)
    session.revision += 1
    session.dirty = True
    session.asm_warm = {}  # re-seed mate solve from the restored transforms
    result = _rebuild(session, emit)
    return _ops_response(session, result, {})


# =========================================================================== Wave 4C derived relink
#
# Cross-document derived-paste LIVE relink. paste_feature_as_derived (engine.doc.ops) does a ONE-TIME
# resolve+stamp: it copies a feature (and its closure) from a SOURCE document and stamps each pasted
# feature with ``derived_from = {doc_id, doc_name, feature, source_feature, group}``. SolidWorks
# derived-part behavior is that when the SOURCE feature changes, the derived copy UPDATES.
#
# refresh_derived is the on-demand re-pull (Option A — an explicit "Rebuild derived" the host calls,
# e.g. on doc re-open or a toolbar button). It is honest SW-ish behavior without a fragile
# auto-cascade across sessions: a derived feature re-pulls its definition from the LIVE source
# document only when asked. It is a MUTATING operation — it pushes one undo snapshot and bumps the
# revision exactly like an op batch, so a single Ctrl+Z reverts a refresh.
#
# Auto-on-rebuild (Option B) was rejected: _rebuild is the pure compile path (compile_document over
# session.doc) and is registry-blind. Reaching into OTHER sessions from inside compile to re-pull
# definitions would (a) couple the compiler to the live session registry, (b) risk reentrancy /
# cross-session ordering hazards (a source whose own rebuild has not run yet), and (c) cannot bump
# the revision / push undo from within compile. refresh_derived keeps the cross-session fetch in the
# session layer where get_session lives and where the mutate-bump-rebuild cadence already exists.
#
# Failure handling (selectors.md "never silently wrong"):
#   * source doc CLOSED/absent (get_session miss)  -> that group is left at its last-resolved
#     definition and marked ``derived_stale`` ({reason:"source_doc_closed"}); no crash.
#   * source FEATURE deleted in the source doc      -> the group's root cannot be bundled, so the
#     group is left as-is and marked ``derived_stale`` ({reason:"source_feature_deleted"}); the
#     per-feature splice (ops.refresh_derived_group) also marks any closure member whose source id
#     vanished. No crash.


def _derived_groups(doc: dict) -> dict[str, dict]:
    """Map each derived ``group`` id present in *doc* to a descriptor
    ``{group, source_doc_id, source_doc_name, members:[{id, source_feature, is_root}]}``.

    A group is the set of features pasted by ONE ``paste_feature_as_derived`` call (they share the
    ``derived_from.group`` stamp). Each member also carries the live ``source_feature`` id it was
    bundled from; :func:`_group_root_source` recovers which one is the closure root for re-bundling."""
    groups: dict[str, dict] = {}
    for f in doc.get("features", []) or []:
        if not isinstance(f, dict):
            continue
        df = f.get("derived_from")
        if not isinstance(df, dict):
            continue
        grp = df.get("group")
        if not isinstance(grp, str):
            continue
        rec = groups.setdefault(grp, {
            "group": grp,
            "source_doc_id": df.get("doc_id"),
            "source_doc_name": df.get("doc_name"),
            "members": [],
        })
        rec["members"].append({"id": f.get("id"),
                               "source_feature": df.get("source_feature"),
                               "is_root": bool(df.get("is_root"))})
    return groups


def _group_root_source(group_rec: dict, source_doc: dict) -> str | None:
    """The source feature id to re-bundle a group from: the member whose source feature is the
    dependency-CLOSURE ROOT in the source doc — i.e. the one that (transitively) references every
    other member's source feature. We recover it by re-walking each still-present candidate's
    closure in the source doc and picking the one whose closure COVERS the most of the group's
    source features (the original paste bundled the root's full closure, so the root's closure is a
    superset of the others'). Ties break to the larger closure (the deeper root)."""
    from engine.doc.ops import _feature_refs_closure
    src_ids = [m["source_feature"] for m in group_rec["members"]
               if isinstance(m.get("source_feature"), str)]
    if not src_ids:
        return None
    src_set = set(src_ids)
    best: str | None = None
    best_key = (-1, -1)
    for cand in src_ids:
        if _find_feature_in(source_doc, cand) is None:
            continue
        try:
            _order, closure = _feature_refs_closure(source_doc, cand)
        except Exception:  # noqa: BLE001
            continue
        key = (len(src_set & closure), len(closure))
        if key > best_key:
            best_key = key
            best = cand
    return best


def _find_feature_in(doc: dict, fid: str):
    for f in doc.get("features", []) or []:
        if isinstance(f, dict) and f.get("id") == fid:
            return f
    return None


def _resolve_source_session(source_doc_id) -> "DocSession | None":
    """Find the live source session for a derived link's ``source_doc_id``.

    The host stamps ``source_doc_id`` from whatever it knows the source by. Two id spaces exist: the
    SESSION registry key (``session.doc_id``, the worker's ``doc_id``) and the DOCUMENT's internal
    id (``doc["id"]``, what build_feature_bundle records by default). We resolve against BOTH so a
    refresh works regardless of which the host stamped — and returns None (source closed/absent) if
    neither matches, so the caller can mark the group stale rather than crash."""
    if not source_doc_id:
        return None
    s = _REGISTRY.get(source_doc_id)
    if s is not None:
        return s
    for cand in _REGISTRY.values():
        if cand.doc.get("id") == source_doc_id:
            return cand
    return None


def _mark_group_stale(session: DocSession, group: str, reason: str,
                      detail: dict | None = None) -> list[str]:
    """Stamp ``derived_stale`` on every member of *group* in the session doc (no geometry change)."""
    marked: list[str] = []
    for f in session.doc.get("features", []) or []:
        if not isinstance(f, dict):
            continue
        df = f.get("derived_from")
        if isinstance(df, dict) and df.get("group") == group:
            f["derived_stale"] = {"reason": reason, **(detail or {})}
            marked.append(f.get("id"))
    return marked


def refresh_derived(session: DocSession, id: str | None = None,
                    emit: EmitFn | None = None) -> dict:
    """Re-pull derived feature definitions from their LIVE source documents (Wave 4C).

    ``id`` (optional): refresh only the derived group that feature ``id`` belongs to. When omitted,
    refresh EVERY derived group in the document. Each group's source feature (and its closure) is
    re-bundled from the live source session and spliced back over the existing derived features in
    place (ids + tree positions preserved) via :func:`engine.doc.ops.refresh_derived_group`.

    Mutating: pushes one undo snapshot, bumps the revision, and rebuilds — like any op batch.
    Returns the ops-response with an extra ``derived`` block summarizing what changed per group.

    Never crashes on a missing source: a closed source doc or a deleted source feature leaves the
    derived group at its last-good definition and stamps ``derived_stale`` (selectors.md)."""
    from engine.doc.ops import build_feature_bundle, refresh_derived_group, _IdGen

    groups = _derived_groups(session.doc)

    if id is not None:
        # Validate the targeted feature is derived FIRST (independent of whether the doc has other
        # derived groups), then narrow to just its group.
        target = _find_feature_in(session.doc, id)
        df = target.get("derived_from") if isinstance(target, dict) else None
        if not (isinstance(df, dict) and isinstance(df.get("group"), str)
                and df["group"] in groups):
            raise OpError("invalid_op",
                          f"refresh_derived: feature '{id}' is not a derived feature",
                          {"reason": "not derived", "id": id})
        groups = {df["group"]: groups[df["group"]]}

    if not groups:
        # Nothing derived — a cheap no-op rebuild (no revision bump), still a full response.
        result = _rebuild(session, emit)
        resp = _ops_response(session, result, {})
        resp["derived"] = {"groups": [], "note": "no derived features in document"}
        return resp

    # Snapshot once up front so the whole multi-group refresh is a SINGLE undo step.
    session._push_undo()
    idgen = _IdGen(session.doc)
    summaries: list[dict] = []
    any_change = False

    for grp, rec in groups.items():
        src_doc_id = rec.get("source_doc_id")
        # 1) Source doc closed/absent -> mark stale, leave last-good.
        src_session = _resolve_source_session(src_doc_id)
        if src_session is None:
            marked = _mark_group_stale(session, grp, "source_doc_closed",
                                       {"doc_id": src_doc_id,
                                        "doc_name": rec.get("source_doc_name")})
            any_change = any_change or bool(marked)
            summaries.append({"group": grp, "status": "source_doc_closed",
                              "source_doc_id": src_doc_id, "stale": marked})
            continue

        # 2) Source feature deleted -> the group's root cannot be bundled. Mark stale, leave as-is.
        root_src = _group_root_source(rec, src_session.doc)
        if root_src is None or _find_feature_in(src_session.doc, root_src) is None:
            marked = _mark_group_stale(session, grp, "source_feature_deleted",
                                       {"doc_id": src_doc_id,
                                        "doc_name": rec.get("source_doc_name")})
            any_change = any_change or bool(marked)
            summaries.append({"group": grp, "status": "source_feature_deleted",
                              "source_doc_id": src_doc_id, "stale": marked})
            continue

        # 3) Re-bundle from the live source root and splice it over the group in place.
        try:
            bundle = build_feature_bundle(src_session.doc, root_src, include_dependencies=True)
            res = refresh_derived_group(session.doc, grp, bundle, idgen)
        except OpError as exc:
            marked = _mark_group_stale(session, grp, "refresh_failed",
                                       {"doc_id": src_doc_id, "error": exc.message})
            any_change = any_change or bool(marked)
            summaries.append({"group": grp, "status": "refresh_failed",
                              "source_doc_id": src_doc_id, "error": exc.message,
                              "stale": marked})
            continue
        any_change = True
        summaries.append({"group": grp, "status": "refreshed", "source_doc_id": src_doc_id,
                          **res})

    # Validate the spliced document (mirror ops.apply_ops' resulting-doc validation). On failure,
    # ROLL BACK (pop the snapshot we just pushed, restore the prior doc) and raise — no half-applied
    # refresh ever commits.
    from engine.doc.schema import validate_document
    errs = validate_document(session.doc)
    if errs:
        import json
        prior = session.undo_stack.pop()
        session.doc = json.loads(prior)
        raise OpError("invalid_op",
                      "refresh_derived produced an invalid document: " + "; ".join(errs[:5]),
                      {"reason": errs[0] if errs else "invalid document", "all_errors": errs})

    if not any_change:
        # Pure no-op (e.g. every group already stale and unchanged): drop the undo snapshot we
        # pushed and don't bump the revision — refresh should not record a phantom step.
        session.undo_stack.pop()
        result = _rebuild(session, emit)
        resp = _ops_response(session, result, {})
        resp["derived"] = {"groups": summaries}
        return resp

    # The spliced features may newly profile a sketch region whose upstream sketch would be a cache
    # hit (same targeted clear apply_ops uses for region-selector batches).
    session.compile_cache.clear()
    if session.active_sketch is not None:
        session.active_sketch.solve_state = None
    session.revision += 1
    session.dirty = True
    result = _rebuild(session, emit)
    resp = _ops_response(session, result, {})
    resp["derived"] = {"groups": summaries}
    return resp


# =========================================================================== C20 assembly methods
#
# Re-solve mates (doc_solve_mates) and explode-state toggle (doc_set_explode_state). solve_mates
# accepts an optional drag {component, target[3]} for free-DOF move; it re-solves, persists the
# new transforms into the doc, bumps the revision (one undoable step), and returns an ops-response.


def solve_mates(session: DocSession, drag: dict | None = None,
                emit: EmitFn | None = None) -> dict:
    """Re-solve the assembly mates (optionally with a drag soft-target) and return an ops-response.

    A solve is a document mutation (component transforms change), so it pushes an undo snapshot and
    bumps the revision exactly like an op batch."""
    if not session.is_assembly:
        raise OpError("invalid_op", "doc_solve_mates: document is not an assembly",
                      {"reason": "not an assembly"})
    from engine.doc import assembly as asm
    base_dir = os.path.dirname(session.path) if session.path else None
    session._push_undo()
    ares = asm.compile_assembly(session.doc, base_dir=base_dir,
                                warm=session.asm_warm or None) if drag is None else \
        _compile_assembly_drag(session, asm, base_dir, drag)
    for comp in session.doc.get("components", []):
        if comp["id"] in ares.transforms:
            comp["transform"] = ares.transforms[comp["id"]]
    session.asm_result = ares
    session.asm_warm = ares.warm
    session.revision += 1
    session.dirty = True
    if session.last_result is None:
        session.last_result = CompileResult()
    return _ops_response(session, session.last_result, {})


def _compile_assembly_drag(session: DocSession, asm, base_dir, drag: dict):
    """compile_assembly with a drag soft-target threaded through to the mate solver."""
    # compile_assembly doesn't take a drag arg directly; replicate its solve with drag by calling
    # the mate solver inside a thin wrapper. We reuse compile_assembly for load+resolve, then a
    # second solve_mates pass with the drag target using the warm result.
    ares = asm.compile_assembly(session.doc, base_dir=base_dir, warm=session.asm_warm or None)
    # Now re-solve with the drag soft target, reusing freshly-loaded geometry by going through the
    # public mate solver against the doc's (now solved) transforms as the warm start.
    from engine.doc.mate_solver import solve_mates as _solve
    components = [c for c in session.doc.get("components", []) if not c.get("suppressed")]
    loaded_ok = {b.component_id for b in ares.bodies}
    solve_components = [
        {"id": c["id"], "transform": ares.transforms.get(c["id"], c.get("transform")),
         "fixed": bool(c.get("fixed", False)), "suppressed": False}
        for c in components if c["id"] in loaded_ok
    ]
    # rebuild resolved geometry the same way compile_assembly did
    resolved_geometry = _asm_resolved_geometry(session.doc, base_dir, asm)
    res = _solve(solve_components, session.doc.get("mates", []), resolved_geometry,
                 warm=ares.warm, drag=drag, time_budget_ms=120.0)
    # repackage into an AssemblyResult-like object using ares as the template
    ares.transforms = res.transforms
    ares.warm = {cid: res.params[cid] for cid in res.params}
    ares.dof = res.dof
    ares.over_defined = res.over_defined
    ares.converged = res.converged
    ares.dof_summary = res.summary
    ares.mate_status = [{"id": m.mate_id, "type": m.type, "status": m.status,
                         "dangling_refs": []} for m in res.mate_status]
    for b in ares.bodies:
        b.transform = list(res.transforms.get(b.component_id, b.transform))
    return ares


def _asm_resolved_geometry(doc: dict, base_dir, asm) -> dict:
    """Resolve every mate ref to a local-frame MateGeom (mirror of compile_assembly's pass)."""
    loaded = {}
    for comp in doc.get("components", []):
        if comp.get("suppressed"):
            continue
        try:
            loaded[comp["id"]] = asm.load_component(comp["source"]["path"], base_dir)
        except Exception:  # noqa: BLE001
            pass
    resolved = {}
    for mate in doc.get("mates", []):
        if mate.get("suppressed"):
            continue
        mid = mate["id"]
        for i, ref in enumerate(mate.get("refs", [])[:2]):
            lc = loaded.get(ref.get("component"))
            sel_obj = ref.get("selector")
            if lc is None or not isinstance(sel_obj, dict):
                continue
            geom = asm._selector_to_mategeom(sel_obj, lc)
            if geom is not None:
                resolved[(mid, i)] = geom
    return resolved


def set_explode_state(session: DocSession, active: bool, factor: float,
                      emit: EmitFn | None = None) -> dict:
    """Toggle exploded-view state and re-tessellate (no revision bump — view-only state)."""
    if not session.is_assembly:
        raise OpError("invalid_op", "doc_set_explode_state: document is not an assembly",
                      {"reason": "not an assembly"})
    session.explode_active = bool(active)
    session.explode_factor = max(0.0, min(1.0, float(factor)))
    if session.asm_result is None:
        _rebuild(session, emit)
    if session.last_result is None:
        session.last_result = CompileResult()
    return _ops_response(session, session.last_result, {})


# =========================================================================== CONFIG configurations
#
# Named configurations: variants of one document driven by per-config feature-param overrides +
# feature/component suppression (the SolidWorks "Configurations" feature). The doc stores
#   configurations: [{name, param_overrides:{"fid.key": value|{value,expr}}, suppressions:{fid: bool}}]
#   active_configuration: <name>
# ABSENT configurations => one implicit "Default" config (today's behavior). Activating/editing the
# ACTIVE config rebuilds (geometry changes); editing an inactive config is a cheap metadata write
# that still bumps the revision (one undoable step) but reuses the last build. All config edits go
# through _config_commit, which validates the resulting doc, pushes one undo snapshot, bumps the
# revision, and (when geometry could change) rebuilds.


def _ensure_configs(doc: dict) -> list[dict]:
    """The doc's configurations list, materializing the implicit Default on first real edit.

    A config-less document carries no ``configurations`` key (so it stays byte-identical until the
    user first touches configs). The first config edit creates an explicit ``Default`` config so the
    list is never empty and ``active_configuration`` always names a real entry — mirroring SolidWorks,
    where a part always has at least one (Default) configuration once you open the ConfigurationManager."""
    configs = doc.get("configurations")
    if not isinstance(configs, list):
        configs = []
        doc["configurations"] = configs
    if not configs:
        configs.append({"name": "Default", "param_overrides": {}, "suppressions": {}})
        doc.setdefault("active_configuration", "Default")
    return configs


def _config_snapshot(doc: dict):
    """Deep-copy the pre-edit (configurations, active_configuration) for rollback on invalid edits."""
    cfgs = doc.get("configurations")
    return (copy.deepcopy(cfgs) if cfgs is not None else None,
            doc.get("active_configuration"))


def _find_config(doc: dict, name: str) -> dict:
    for c in doc.get("configurations", []) or []:
        if isinstance(c, dict) and c.get("name") == name:
            return c
    raise OpError("invalid_op", f"no configuration named '{name}'",
                  {"reason": "unknown configuration", "name": name})


def _config_active_name(doc: dict) -> str:
    """The active config name, honoring the implicit-Default + first-config fallbacks."""
    from engine.doc.compiler import resolve_active_config
    return resolve_active_config(doc)[0]


def config_list(session: DocSession) -> dict:
    """Read-only: configuration names + the active one (worker_rpc config_list). Never rebuilds.

    For a config-less document this reports the single implicit ``["Default"]`` with
    ``active="Default"`` so the host always has at least one config to show."""
    configs = session.doc.get("configurations")
    if isinstance(configs, list) and configs:
        names = [c.get("name") for c in configs if isinstance(c, dict)]
        active = _config_active_name(session.doc)
    else:
        names = ["Default"]
        active = "Default"
    # Echo each config's override/suppression payload so the host can render the config table.
    detail = []
    for c in (configs or []):
        if isinstance(c, dict):
            detail.append({"name": c.get("name"),
                           "param_overrides": c.get("param_overrides", {}) or {},
                           "suppressions": c.get("suppressions", {}) or {}})
    if not detail:
        detail = [{"name": "Default", "param_overrides": {}, "suppressions": {}}]
    return {"configurations": names, "active": active, "detail": detail}


def _config_commit(session: DocSession, *, rebuild_needed: bool,
                   emit: EmitFn | None = None) -> dict:
    """Validate the edited doc, push one undo step, bump the revision, and (re)build.

    ``rebuild_needed`` True forces a fresh compile of the (now possibly different) active config;
    False reuses the last build (an inactive-config metadata edit) but still emits the ops-response
    so the host refreshes its config table. Validation failure raises OpError WITHOUT mutating the
    session (the caller already mutated session.doc, so we must validate BEFORE this is called and
    let the OpError propagate — see callers, which validate first)."""
    session._push_undo()
    session.revision += 1
    session.dirty = True
    if rebuild_needed:
        # Switching/editing the active config changes geometry — drop warm sketch state (the active
        # sketch may now solve different seeds) and recompile. Clear the compile cache first: a
        # region-consuming feature (extrude/revolve/sweep/loft) reads its profile from the upstream
        # sketch's compiler side-channel, which a CACHE-HIT sketch does not repopulate. Switching a
        # config recompiles the changed (override/suppress) features while their unchanged sketch
        # would be a hit, so without this clear the consumer would see "sketch not compiled". Same
        # targeted clear apply_ops uses for region-selector batches.
        session.compile_cache.clear()
        if session.active_sketch is not None:
            session.active_sketch.solve_state = None
        result = _rebuild(session, emit)
    else:
        result = session.last_result if session.last_result is not None else _rebuild(session, emit)
    return _ops_response(session, result, {})


def _validate_or_rollback(session: DocSession, before_configs, before_active) -> None:
    """Validate the edited document; on failure RESTORE the pre-edit config state and raise.

    A config mutator edits ``session.doc`` in place (it holds direct references into the live
    configurations). If the result is invalid (e.g. an override naming a non-existent feature), we
    must not leave the session document in a broken state — so the mutator passes the deep-copied
    pre-edit ``configurations`` + ``active_configuration`` and we revert them before raising."""
    from engine.doc.schema import validate_document
    errs = validate_document(session.doc)
    if errs:
        if before_configs is None:
            session.doc.pop("configurations", None)
        else:
            session.doc["configurations"] = before_configs
        if before_active is None:
            session.doc.pop("active_configuration", None)
        else:
            session.doc["active_configuration"] = before_active
        raise OpError("invalid_op", "configuration edit produced an invalid document: "
                      + "; ".join(errs[:5]), {"reason": "invalid configuration", "errors": errs})


def config_add(session: DocSession, name: str, copy_from: str | None = None,
               emit: EmitFn | None = None) -> dict:
    """Add a new configuration (worker_rpc config_add). ``copy_from`` (optional) seeds the new
    config's overrides/suppressions from an existing one. Does NOT activate it (no geometry change),
    so it reuses the last build. Duplicate names are a clean error."""
    if not isinstance(name, str) or not name:
        raise OpError("invalid_op", "config_add: 'name' must be a non-empty string",
                      {"reason": "bad name"})
    before = _config_snapshot(session.doc)
    configs = _ensure_configs(session.doc)
    if any(c.get("name") == name for c in configs):
        raise OpError("invalid_op", f"config_add: configuration '{name}' already exists",
                      {"reason": "duplicate name", "name": name})
    seed = {"param_overrides": {}, "suppressions": {}}
    if copy_from is not None:
        src = _find_config(session.doc, copy_from)
        seed = {"param_overrides": copy.deepcopy(src.get("param_overrides", {}) or {}),
                "suppressions": copy.deepcopy(src.get("suppressions", {}) or {})}
    configs.append({"name": name, **seed})
    _validate_or_rollback(session, *before)
    return _config_commit(session, rebuild_needed=False, emit=emit)


def config_delete(session: DocSession, name: str, emit: EmitFn | None = None) -> dict:
    """Delete a configuration (worker_rpc config_delete). Deleting the ACTIVE config falls the
    active pointer back to the first remaining config and rebuilds. The LAST config cannot be
    deleted (a document always keeps at least one configuration)."""
    configs = session.doc.get("configurations")
    if not isinstance(configs, list) or not configs:
        raise OpError("invalid_op", f"config_delete: no configuration '{name}' to delete",
                      {"reason": "no configurations"})
    if not any(c.get("name") == name for c in configs):
        raise OpError("invalid_op", f"config_delete: no configuration named '{name}'",
                      {"reason": "unknown configuration", "name": name})
    if len(configs) == 1:
        raise OpError("invalid_op", "config_delete: cannot delete the only configuration",
                      {"reason": "last configuration"})
    before = _config_snapshot(session.doc)
    was_active = _config_active_name(session.doc) == name
    session.doc["configurations"] = [c for c in configs if c.get("name") != name]
    if was_active:
        session.doc["active_configuration"] = session.doc["configurations"][0].get("name")
    _validate_or_rollback(session, *before)
    return _config_commit(session, rebuild_needed=was_active, emit=emit)


def config_activate(session: DocSession, name: str, emit: EmitFn | None = None) -> dict:
    """Activate a configuration (worker_rpc config_activate) → rebuild + ops-response for the new
    active config (tree/rebuild/tess reflect its overrides + suppressions)."""
    before = _config_snapshot(session.doc)
    _ensure_configs(session.doc)
    _find_config(session.doc, name)  # raises if unknown
    session.doc["active_configuration"] = name
    _validate_or_rollback(session, *before)
    return _config_commit(session, rebuild_needed=True, emit=emit)


def config_set_override(session: DocSession, name: str, feature_id: str, param: str,
                        value: Any, clear: bool = False, emit: EmitFn | None = None) -> dict:
    """Set or clear a per-config param override (worker_rpc config_set_override).

    ``value`` is the override (a scalar, or a ``{value, expr}`` dim object); pass ``clear=True``
    (or ``value=None`` with no expr) to REMOVE the override so the base param value is used. Editing
    the active config rebuilds; editing an inactive config is a metadata-only write."""
    before = _config_snapshot(session.doc)
    cfg = _find_config(session.doc, name)
    key = f"{feature_id}.{param}"
    overrides = cfg.setdefault("param_overrides", {})
    if clear or value is None:
        overrides.pop(key, None)
    else:
        overrides[key] = value
    _validate_or_rollback(session, *before)
    rebuild_needed = _config_active_name(session.doc) == name
    return _config_commit(session, rebuild_needed=rebuild_needed, emit=emit)


def config_set_suppression(session: DocSession, name: str, feature_id: str,
                           suppressed: bool, emit: EmitFn | None = None) -> dict:
    """Set a per-config feature/component suppression (worker_rpc config_set_suppression).

    Setting ``suppressed=False`` records an explicit un-suppress (the feature compiles even if its
    base is suppressed-by-config elsewhere)? No — the overlay is base.suppressed OR config flag, so
    a False entry is a no-op vs. absent; we therefore DROP a False entry to keep configs minimal.
    Editing the active config rebuilds."""
    before = _config_snapshot(session.doc)
    cfg = _find_config(session.doc, name)
    sup = cfg.setdefault("suppressions", {})
    if suppressed:
        sup[feature_id] = True
    else:
        sup.pop(feature_id, None)
    _validate_or_rollback(session, *before)
    rebuild_needed = _config_active_name(session.doc) == name
    return _config_commit(session, rebuild_needed=rebuild_needed, emit=emit)


# --------------------------------------------------------------------------- resolve pick


def _ensure_built(session: DocSession) -> CompileResult:
    if session.is_assembly:
        if session.asm_result is None:
            _rebuild(session)
        if session.last_result is None:
            session.last_result = CompileResult()
        return session.last_result
    if session.last_result is None:
        _rebuild(session)
    assert session.last_result is not None
    return session.last_result


def asm_world_bodies(session: DocSession, explode: bool = False) -> list[tuple[str, Any]]:
    """World-placed ``(component_name, shape)`` for each assembly component (interference / BOM).

    ``explode=False`` ignores the explode offset (interference/mass measure the real assembly).
    """
    from engine.doc import assembly as asm
    import numpy as np
    if session.asm_result is None:
        _rebuild(session)
    ares = session.asm_result
    if ares is None:
        return []
    offsets = asm.explode_offsets(session.doc, session.explode_factor) \
        if (explode and session.explode_active) else {}
    out: list[tuple[str, Any]] = []
    for body in ares.bodies:
        off = offsets.get(body.component_id)
        world = asm.placed_world_shape(body, np.asarray(off) if off is not None else None)
        out.append((body.name, world))
    return out


def asm_component_bodies(session: DocSession) -> list[dict]:
    """Per-REAL-component local-shape list for BOM (name, source path, shape, quantity) — mass
    measured locally (mass is transform-invariant, so no need to world-place).

    Synthetic component-pattern instances (assembly.py appends them to ``ares.bodies`` with a
    ``"<seedcid>#<k>"`` component_id and ``body_id >= 1_000_000``) are NOT distinct line items:
    SolidWorks rolls a patterned instance into its SEED part's quantity. So we SKIP the instances
    here and instead carry, per real component, ``quantity = 1 + (number of pattern instances seeded
    from it)`` — the BOM rolls that up by source. Skipping them also keeps the returned list indexed
    1:1 with ``doc["components"]`` order, which the mass evaluator (worker._eval_active_shape) relies
    on for its ``[body_id - 1]`` lookup."""
    if session.asm_result is None:
        _rebuild(session)
    ares = session.asm_result
    if ares is None:
        return []
    # Count pattern instances seeded from each real component (component_id "<seedcid>#<k>").
    instance_counts: dict[str, int] = {}
    for body in ares.bodies:
        cid = body.component_id
        if "#" in cid:
            seed = cid.split("#", 1)[0]
            instance_counts[seed] = instance_counts.get(seed, 0) + 1
    out: list[dict] = []
    by_id = {c["id"]: c for c in session.doc.get("components", [])}
    for body in ares.bodies:
        if "#" in body.component_id:
            continue  # synthetic pattern instance — folded into its seed's quantity
        comp = by_id.get(body.component_id, {})
        out.append({
            "component": body.component_id,
            "name": body.name,
            "source": (comp.get("source") or {}).get("path", ""),
            "shape": body.shape_local,
            "quantity": 1 + instance_counts.get(body.component_id, 0),
        })
    return out


def _bbox_diag(shape) -> float:
    try:
        bb = shape.bounding_box()
        dx = bb.max.X - bb.min.X
        dy = bb.max.Y - bb.min.Y
        dz = bb.max.Z - bb.min.Z
        import math
        return max(math.sqrt(dx * dx + dy * dy + dz * dz), 1e-3)
    except Exception:  # noqa: BLE001
        return 100.0


def resolve_pick(session: DocSession, revision: int | None,
                 body_id: int, kind: str, topo_id: int) -> dict:
    """Build a persistent selector for the picked topology element (worker_rpc.md doc_resolve_pick).

    ``body_id`` is the 1-based body ordinal in the rebuild (matches the AICM BODY ids written by
    :func:`_write_aicm`). ``topo_id`` is the 1-based TopExp map index of the face/edge/vertex.
    """
    if revision is not None and revision != session.revision:
        raise OpError("stale_revision",
                      f"revision {revision} != current {session.revision}",
                      {"current_revision": session.revision})

    if session.is_assembly:
        return _resolve_pick_assembly(session, body_id, kind, topo_id)

    result = _ensure_built(session)
    bodies = _solid_bodies(result)
    if body_id < 1 or body_id > len(bodies):
        raise OpError("invalid_op", f"body_id {body_id} out of range (1..{len(bodies)})",
                      {"reason": "bad body_id"})
    bo = bodies[body_id - 1]
    shape = bo.shape

    if kind == "body":
        return {"selector": {"kind": "body", "owner_feature": _body_owner(result, bo)}}

    diag = _bbox_diag(shape)
    raw = shape.wrapped

    if kind == "face":
        infos = sel.describe_faces(raw)
        owners = result.face_owners.get(bo.body_id, {})
    elif kind == "edge":
        infos = sel.describe_edges(raw)
        owners = result.edge_owners.get(bo.body_id, {})
    elif kind == "vertex":
        infos = sel.describe_vertices(raw)
        owners = {}  # vertices have no compiler owner map; selector owner stays None
    else:
        raise OpError("invalid_op", f"kind '{kind}' must be face|edge|vertex|body",
                      {"reason": "bad kind"})

    if topo_id < 1 or topo_id > len(infos):
        raise OpError("invalid_op",
                      f"topo_id {topo_id} out of range (1..{len(infos)}) for {kind}",
                      {"reason": "bad topo_id"})
    info = infos[topo_id - 1]
    # Stamp the owner from the compiler's index-keyed owner map (topo_id is 1-based).
    owner = owners.get(topo_id)
    if owner is not None:
        info.owner = owner
    info.hint_index = topo_id - 1

    selector = sel.make_selector(info)
    return {"selector": selector}


def _resolve_pick_assembly(session: DocSession, body_id: int, kind: str, topo_id: int) -> dict:
    """Resolve a pick in an assembly: body_id = component ordinal, topo_id indexes that component's
    LOCAL topology (TopExp order is transform-invariant, so the world AICM ids match local ids).

    Returns ``{selector, component}`` so the host can build a mate ref ``{component, selector}``."""
    _rebuild_if_needed(session)
    ares = session.asm_result
    if ares is None or body_id < 1 or body_id > len(ares.bodies):
        raise OpError("invalid_op", f"body_id {body_id} out of range for assembly",
                      {"reason": "bad body_id"})
    abody = ares.bodies[body_id - 1]
    shape_local = abody.shape_local

    if kind == "body":
        return {"selector": {"kind": "body", "owner_feature": ""}, "component": abody.component_id}

    raw = shape_local.wrapped
    if kind == "face":
        infos = sel.describe_faces(raw)
    elif kind == "edge":
        infos = sel.describe_edges(raw)
    elif kind == "vertex":
        infos = sel.describe_vertices(raw)
    else:
        raise OpError("invalid_op", f"kind '{kind}' must be face|edge|vertex|body",
                      {"reason": "bad kind"})
    if topo_id < 1 or topo_id > len(infos):
        raise OpError("invalid_op",
                      f"topo_id {topo_id} out of range (1..{len(infos)}) for {kind}",
                      {"reason": "bad topo_id"})
    info = infos[topo_id - 1]
    info.hint_index = topo_id - 1
    selector = sel.make_selector(info)
    return {"selector": selector, "component": abody.component_id}


def _rebuild_if_needed(session: DocSession) -> None:
    if session.asm_result is None:
        _rebuild(session)


def _body_owner(result: CompileResult, bo) -> str:
    """A representative owner feature for a whole-body selector: the most common face owner."""
    owners = result.face_owners.get(bo.body_id, {})
    if not owners:
        return ""
    counts: dict[str, int] = {}
    for v in owners.values():
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)


# =========================================================================== C18 sketch mode
#
# Per-session active-sketch interaction (worker_rpc.md §2 sketch_*, doc_schema.md §3). One sketch
# is "entered" at a time (SolidWorks behaviour). sketch_enter resolves the sketch plane, builds the
# equation env, warm-solves via engine.doc.sketch_solver (reports.md §C10), and returns solved
# coords + per-entity statuses + DOF + a 16-float row-major plane_frame mapping sketch (u,v,0) to
# world. sketch_drag is TRANSIENT (no revision bump, warm-started). sketch_drag_commit persists the
# solved coords as ONE undoable sketch_edit_entity batch. sketch_exit clears state and rebuilds.


@dataclass
class _SketchState:
    sketch_fid: str
    solve_state: Any = None                      # sketch_solver.SolveState (warm start)
    last_entities: dict = field(default_factory=dict)   # eid -> solved param dict
    last_statuses: dict = field(default_factory=dict)   # eid -> under|full|over
    last_overall_status: Optional[str] = None    # under|full|over (doc_status.active_sketch)
    plane_frame: list = field(default_factory=list)     # 16 floats, row-major


def _find_sketch_feature(session: DocSession, sketch_fid: str) -> dict:
    for f in session.doc.get("features", []):
        if f.get("id") == sketch_fid:
            if f.get("type") != "sketch":
                raise OpError("invalid_op", f"feature '{sketch_fid}' is not a sketch",
                              {"reason": "not a sketch"})
            return f
    raise OpError("invalid_op", f"no sketch feature '{sketch_fid}'", {"reason": "unknown sketch"})


def _equations_env(session: DocSession) -> dict:
    """Resolved global equation env (expression dims look up here). Empty on equation error."""
    from engine.doc.equations import evaluate_globals, EquationError
    globs = session.doc.get("equations", {}).get("globals", []) or []
    try:
        return evaluate_globals(globs)
    except EquationError:
        return {}


def _resolve_sketch_plane(session: DocSession, sketch_feat: dict, result: CompileResult):
    """Resolve the sketch's refs.plane to a build123d Plane using the compiler's resolver.

    Builds a CompileContext carrying the reserved datums, any datum-feature datums (re-resolved
    from the doc), and the last compiled bodies, then calls sketch._resolve_plane_ref. Face
    selectors resolve against the last solid body (same path the compiler uses on rebuild).
    """
    from engine.doc.features import sketch as sketch_mod
    from engine.doc.features.base import CompileContext, get_compiler
    from engine.doc.features.datum import reserved_datums

    plane_ref = sketch_feat.get("refs", {}).get("plane")
    if plane_ref is None:
        raise OpError("invalid_op", "sketch has no refs.plane", {"reason": "no plane"})

    env = result.equations_env or {}
    datums = reserved_datums()
    bodies = [bo.shape for bo in result.bodies]

    # Re-resolve upstream datum features so datum-feature plane refs work (offset/midplane etc.).
    for f in session.doc.get("features", []):
        if f.get("id") == sketch_feat.get("id"):
            break
        if f.get("type") in ("plane", "axis", "point", "csys") and not f.get("suppressed"):
            comp = get_compiler(f["type"])
            if comp is None:
                continue
            try:
                ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums)
                out = comp(f, ctx)
                if out.datum is not None:
                    datums[f["id"]] = out.datum
            except Exception:  # noqa: BLE001
                pass

    ctx = CompileContext(equations_env=env, bodies=bodies, datums=datums)
    try:
        return sketch_mod._resolve_plane_ref(plane_ref, ctx)
    except Exception as exc:  # noqa: BLE001
        raise OpError("invalid_op", f"could not resolve sketch plane: {exc}",
                      {"reason": "plane resolve failed"})


def _plane_frame(plane) -> list:
    """16-float row-major matrix mapping sketch (u,v,0,1) -> world. Columns: x_dir, y_dir, z_dir,
    origin. (Matches build123d Plane.from_local_coords; OCP Transformation is 1-indexed.)"""
    t = plane.location.wrapped.Transformation()
    # 3x4 affine rows; append [0,0,0,1] for a homogeneous 4x4.
    rows = [[float(t.Value(i + 1, j + 1)) for j in range(4)] for i in range(3)]
    rows.append([0.0, 0.0, 0.0, 1.0])
    return [v for row in rows for v in row]


def _overall_status(statuses: dict, dof: int, conflicting: list,
                    redundant: list | None = None) -> str:
    # SolidWorks parity: a redundant DRIVING dim makes the sketch Over Defined even when the
    # values are consistent (driven dims never reach the solver, so they can't appear here).
    if conflicting or redundant or any(s == "over" for s in statuses.values()):
        return "over"
    if dof <= 0:
        return "full"
    return "under"


def _sketch_constraints(sketch_feat: dict, entity_coords: dict) -> list[dict]:
    """Pack EVERY constraint in the active sketch for the C++ sketcher's dimension annotations
    (C35-overhaul §2). Each record: ``{id, type, refs, value, expr, driven, placement, measured}``.

    ``measured`` is the current solved measurement for dimensional types (distance/angle/radius/
    diameter), null for geometric relations; ``value``/``expr`` come from the stored dim; ``driven``
    and ``placement`` default to false/null when unset.
    """
    from engine.doc.sketch_solver import measure_constraint
    out: list[dict] = []
    params = sketch_feat.get("params", {}) or {}
    for c in params.get("constraints", []) or []:
        if not isinstance(c, dict):
            continue
        val_obj = c.get("value") if isinstance(c.get("value"), dict) else None
        rec = {
            "id": c.get("id"),
            "type": c.get("type"),
            "refs": list(c.get("refs", []) or []),
            "value": val_obj.get("value") if val_obj else None,
            "expr": val_obj.get("expr") if val_obj else None,
            "driven": bool(c.get("driven", False)),
            "placement": c.get("placement"),
            "measured": None,
        }
        # C50: annotation-affecting dim variants must survive the round trip — the host renders
        # committed dims from THIS record. Without axis the h/v variant drew as an aligned dim;
        # without cond a min/max (rim) distance snapped back to center anchoring after commit
        # (the solver still enforced it — display-only, but reads as the wrong constraint).
        if isinstance(c.get("axis"), str) and c["axis"]:
            rec["axis"] = c["axis"]
        if isinstance(c.get("cond"), list):
            rec["cond"] = list(c["cond"])
        try:
            rec["measured"] = measure_constraint(c, entity_coords)
        except Exception:  # noqa: BLE001
            rec["measured"] = None
        out.append(rec)
    return out


def _doc_entity_coords(params: dict) -> dict:
    """Freeze map: entity id -> its CURRENT stored coords (same key shape solve_sketch returns).

    Used by the conflict-safe path (C50f) — when a solve is rejected (contradictory/non-convergent)
    we ship the document's last-valid geometry instead of the solver's least-squares compromise, so
    the sketch never visually explodes. Strips non-coord bookkeeping keys (id/construction)."""
    out: dict = {}
    for e in params.get("entities", []) or []:
        if not isinstance(e, dict) or "id" not in e:
            continue
        rec = {k: v for k, v in e.items() if k not in ("id", "construction")}
        out[e["id"]] = rec
    return out


def _solve_active(session: DocSession, sketch_feat: dict, warm) -> dict:
    """Solve the active sketch and pack the sketch_enter/solve result dict (minus plane_frame).

    C50f conflict-safe solving: if the solve is REJECTED (``conflicting`` non-empty OR not
    converged) the geometry is frozen at the document's current (pre-solve) coords rather than
    persisting/returning the solver's least-squares compromise. Consistent over-definition
    (``redundant`` non-empty, ``conflicting`` empty, converged) still solves and persists as before.
    The packed dict carries ``rejected`` so callers can skip persistence, drop warm state, and set
    the ``solve_rejected`` response flag."""
    from engine.doc.sketch_solver import solve_sketch
    env = _equations_env(session)
    params = sketch_feat.get("params", {}) or {}
    sol = solve_sketch(params, env, warm=warm)
    # Reject (freeze geometry) when the constraints are CONTRADICTORY: the solver tagged specific
    # conflicting constraints, OR the solve failed to converge at all. A non-converged solve NEVER
    # ships its coordinates — they are a least-squares compromise, not a solution. (An earlier
    # revision exempted non-convergence when dof > 0, assuming contradictions read dof <= 0; both
    # halves of that were wrong: QR counts a contradiction's rank deficiency as slack, so
    # contradictory chains report POSITIVE dof, and the "healthy sketches sometimes don't
    # converge" fear traced back to the sign-flipped point-line Jacobian, fixed in C50g —
    # see scratch/probe_converged_audit.py.)
    rejected = bool(sol.conflicting) or not sol.converged
    # On rejection freeze geometry at the last-valid doc coords; otherwise use the solver output.
    coord_map = _doc_entity_coords(params) if rejected else dict(sol.entities)
    # entities list: coords carrying ids (host renders these directly)
    entities = []
    for eid, coords in coord_map.items():
        rec = dict(coords)
        rec["id"] = eid
        # carry construction flag through for host rendering
        for e in params.get("entities", []):
            if e.get("id") == eid:
                rec["construction"] = bool(e.get("construction", False))
                break
        entities.append(rec)
    return {
        # On rejection clear warm state (None) — the undo-determinism precedent (C50) prefers a cold
        # re-solve over warm-starting from a failed/compromise configuration.
        "_state": None if rejected else sol.state,
        "entities": entities,
        "entity_map": coord_map,
        "statuses": dict(sol.statuses),
        "dof": int(sol.dof),
        "conflicting": list(sol.conflicting),
        "redundant": list(sol.redundant),
        "rejected": rejected,
    }


def sketch_enter(session: DocSession, sketch_fid: str) -> dict:
    """Enter sketch-edit mode: resolve plane, solve, store warm state. (worker_rpc.md sketch_enter)"""
    feat = _find_sketch_feature(session, sketch_fid)
    result = _ensure_built(session)
    plane = _resolve_sketch_plane(session, feat, result)
    frame = _plane_frame(plane)

    packed = _solve_active(session, feat, warm=None)
    st = _SketchState(sketch_fid=sketch_fid)
    st.solve_state = packed["_state"]
    st.last_entities = packed["entity_map"]
    st.last_statuses = packed["statuses"]
    st.last_overall_status = _overall_status(packed["statuses"], packed["dof"],
                                             packed["conflicting"], packed["redundant"])
    st.plane_frame = frame
    session.active_sketch = st

    return {
        "entities": packed["entities"],
        "statuses": packed["statuses"],
        "dof": packed["dof"],
        "plane_frame": frame,
        "status": st.last_overall_status,
        "conflicting": packed["conflicting"],
        "redundant": packed["redundant"],
        "solve_rejected": packed["rejected"],
        "constraints": _sketch_constraints(feat, packed["entity_map"]),
    }


def sketch_overview(session: DocSession, sketch_fid: str) -> dict:
    """C40: read-only snapshot of a sketch (plane frame + solved entity coords) so the host can
    ghost-render inactive sketches in the 3D viewport. Unlike sketch_enter this NEVER touches
    ``session.active_sketch`` — it is safe to call while another sketch is being edited."""
    feat = _find_sketch_feature(session, sketch_fid)
    result = _ensure_built(session)
    plane = _resolve_sketch_plane(session, feat, result)
    packed = _solve_active(session, feat, warm=None)
    return {
        "entities": packed["entities"],
        "statuses": packed["statuses"],
        "dof": packed["dof"],
        "plane_frame": _plane_frame(plane),
        "status": _overall_status(packed["statuses"], packed["dof"],
                                  packed["conflicting"], packed["redundant"]),
    }


def sketch_regions(session: DocSession, sketch_fid: str) -> dict:
    """Read-only enumeration of a sketch's closed-loop regions for the feature-PM viewport overlay.

    Returns ``{plane_frame, bbox_diag, regions:[{index, centroid:[u,v], area, depth, is_material,
    outer:[[u,v]...], holes:[[[u,v]...]...]}]}`` — the same even-odd regions extrude/revolve see,
    so a viewport pick's ``index`` maps 1:1 to the profile the compiler resolves. ``outer``/``holes``
    are sketch-local 2-D polylines; the host lifts them with ``plane_frame``. Never mutates the doc
    or touches ``session.active_sketch`` (safe alongside an open PM)."""
    import math
    from engine.doc.features.sketch import (
        _entity_to_edges, compute_regions, region_triangles_2d,
    )

    feat = _find_sketch_feature(session, sketch_fid)
    result = _ensure_built(session)
    plane = _resolve_sketch_plane(session, feat, result)

    entities = (feat.get("params", {}) or {}).get("entities", []) or []
    all_edges: list = []
    for ent in entities:
        if not isinstance(ent, dict) or ent.get("construction", False):
            continue
        try:
            all_edges.extend(_entity_to_edges(ent))
        except Exception:  # noqa: BLE001
            continue

    faces, descs = compute_regions(all_edges, plane)

    coords: list[float] = []
    for d in descs:
        for pt in d.get("outer", []):
            coords.extend((pt[0], pt[1]))
    span = (max(coords) - min(coords)) if coords else 100.0
    diag = max(span * math.sqrt(2), 1e-3)

    regions = [{
        "index": d["index"],
        "centroid": d["centroid"],
        "area": d.get("area_or_len", 0.0),
        "depth": d.get("depth", 0),
        "is_material": bool(d.get("is_material", True)),
        "outer": d.get("outer", []),
        "holes": d.get("holes", []),
        "tris": region_triangles_2d(f, plane),
    } for d, f in zip(descs, faces)]

    return {
        "plane_frame": _plane_frame(plane),
        "bbox_diag": diag,
        "regions": regions,
    }


# Dimensional constraint types — adding one of these consumes a DOF, so dof_after >= dof_before
# means the new dimension was absorbed by an already-determined system (i.e. it over-defines).
_PROBE_DIM_TYPES = frozenset({"distance", "angle", "radius", "diameter", "length"})
_PROBE_CID = "__probe__"


def sketch_probe(session: DocSession, sketch_fid: str, action: dict) -> dict:
    """C50f over-define PRE-FLIGHT probe. Apply ``action`` to a DEEP COPY of the active sketch's
    params, cold-solve the copy, and report whether the action would over-define — WITHOUT touching
    the document, session sketch state, undo stack, or warm-start state.

    ``action`` is exactly one of:
      ``{"add_constraint": {<constraint json>}}`` — the probe constraint is tagged id ``__probe__``
        so it can be spotted in the conflicting/redundant lists.
      ``{"set_dim": {"id": <cid>, "value": <num>, "expr"?: str, "driven"?: bool}}``
      ``{"delete_constraint": {"id": <cid>}}``

    Returns ``{ok, status, dof_before, dof_after, converged, conflicting, redundant,
    would_over_define}``. On an unmatched sketch or malformed action: ``{ok: False, error}``.
    """
    import copy as _copy
    from engine.doc.sketch_solver import solve_sketch

    # The probe operates on the active sketch session (same context sketch_solve uses).
    try:
        feat = _find_sketch_feature(session, sketch_fid)
    except OpError:
        return {"ok": False, "error": f"no sketch feature '{sketch_fid}'"}
    st = session.active_sketch
    if st is None or st.sketch_fid != sketch_fid:
        return {"ok": False, "error": f"sketch '{sketch_fid}' is not the active sketch"}

    if not isinstance(action, dict) or len(action) != 1:
        return {"ok": False, "error": "action must be exactly one of "
                "add_constraint / set_dim / delete_constraint"}

    env = _equations_env(session)
    base_params = feat.get("params", {}) or {}

    # COLD baseline solve of the unmodified copy (no warm state) for dof_before.
    before = solve_sketch(_copy.deepcopy(base_params), env, warm=None)
    dof_before = int(before.dof)

    # Deep-copy and mutate the COPY only.
    params = _copy.deepcopy(base_params)
    params.setdefault("entities", [])
    cons = params.setdefault("constraints", [])

    probe_is_dim = False
    if "add_constraint" in action:
        con = action["add_constraint"]
        if not isinstance(con, dict):
            return {"ok": False, "error": "add_constraint requires a constraint object"}
        c = _copy.deepcopy(con)
        c["id"] = _PROBE_CID
        cons.append(c)
        probe_is_dim = c.get("type") in _PROBE_DIM_TYPES
    elif "set_dim" in action:
        sd = action["set_dim"]
        if not isinstance(sd, dict) or "id" not in sd:
            return {"ok": False, "error": "set_dim requires an 'id'"}
        cid = sd["id"]
        target = next((c for c in cons if c.get("id") == cid), None)
        if target is None:
            return {"ok": False, "error": f"set_dim: constraint '{cid}' not found"}
        # Mirror ops._op_sketch_set_dim's value/expr/driven handling on the copy.
        if sd.get("expr") is not None:
            cur = target.get("value") if isinstance(target.get("value"), dict) else {"value": 0.0}
            target["value"] = {"value": cur.get("value", 0.0), "expr": sd["expr"]}
        elif sd.get("value") is not None:
            target["value"] = {"value": float(sd["value"]), "expr": None}
        if sd.get("driven") is not None:
            target["driven"] = bool(sd["driven"])
    elif "delete_constraint" in action:
        dc = action["delete_constraint"]
        if not isinstance(dc, dict) or "id" not in dc:
            return {"ok": False, "error": "delete_constraint requires an 'id'"}
        cid = dc["id"]
        if not any(c.get("id") == cid for c in cons):
            return {"ok": False, "error": f"delete_constraint: '{cid}' not found"}
        params["constraints"] = [c for c in cons if c.get("id") != cid]
    else:
        return {"ok": False, "error": "unknown action; expected add_constraint / "
                "set_dim / delete_constraint"}

    after = solve_sketch(params, env, warm=None)
    dof_after = int(after.dof)
    conflicting = list(after.conflicting)
    redundant = list(after.redundant)

    # would_over_define: the probed constraint is flagged conflicting/redundant, OR adding a
    # dimensional constraint failed to reduce the DOF (absorbed by an already-determined system),
    # OR the system no longer converges (contradictory value — note contradictions can read
    # POSITIVE dof: QR counts their rank deficiency as slack, so don't gate on dof here; same
    # rule as the _solve_active rejection test).
    would = (
        _PROBE_CID in conflicting
        or _PROBE_CID in redundant
        or (probe_is_dim and dof_after >= dof_before)
        or not after.converged
        or bool(conflicting)
    )

    return {
        "ok": True,
        "status": _overall_status(dict(after.statuses), dof_after, conflicting, redundant),
        "dof_before": dof_before,
        "dof_after": dof_after,
        "converged": bool(after.converged),
        "conflicting": conflicting,
        "redundant": redundant,
        "would_over_define": bool(would),
    }


def sketch_solve(session: DocSession, sketch_fid: str) -> dict:
    """Re-solve the active sketch (e.g. after a dim edit) and refresh warm state."""
    feat = _find_sketch_feature(session, sketch_fid)
    st = session.active_sketch
    warm = st.solve_state if (st and st.sketch_fid == sketch_fid) else None
    packed = _solve_active(session, feat, warm=warm)
    if st is None or st.sketch_fid != sketch_fid:
        result = _ensure_built(session)
        plane = _resolve_sketch_plane(session, feat, result)
        st = _SketchState(sketch_fid=sketch_fid, plane_frame=_plane_frame(plane))
        session.active_sketch = st
    st.solve_state = packed["_state"]
    st.last_entities = packed["entity_map"]
    st.last_statuses = packed["statuses"]
    st.last_overall_status = _overall_status(packed["statuses"], packed["dof"],
                                             packed["conflicting"], packed["redundant"])
    return {
        "entities": packed["entities"],
        "statuses": packed["statuses"],
        "dof": packed["dof"],
        "plane_frame": st.plane_frame,
        "status": st.last_overall_status,
        "conflicting": packed["conflicting"],
        "redundant": packed["redundant"],
        "solve_rejected": packed["rejected"],
        "constraints": _sketch_constraints(feat, packed["entity_map"]),
    }


def sketch_drag(session: DocSession, sketch_fid: str, handle: str, xy,
                targets: list | None = None) -> dict:
    """Transient warm-started drag (worker_rpc.md sketch_drag). NO revision bump, NO commit.

    Single-point: pass ``handle`` + ``xy``. Whole-entity / profile / multi-select drag: pass
    ``targets`` = list of ``{"handle", "xy":[x,y]}`` to translate many points rigidly at once.
    """
    from engine.doc.sketch_solver import drag as _drag
    feat = _find_sketch_feature(session, sketch_fid)
    st = session.active_sketch
    if st is None or st.sketch_fid != sketch_fid or st.solve_state is None:
        # Not entered (or stale) — enter implicitly so drag is still serviceable.
        sketch_enter(session, sketch_fid)
        st = session.active_sketch
    env = _equations_env(session)
    params = feat.get("params", {}) or {}
    target = (float(xy[0]), float(xy[1])) if xy is not None else None
    res = _drag(st.solve_state, handle, target, params, env, targets=targets)
    # Pack entities with ids + construction flags for the host.
    entities = []
    for eid, coords in res["entities"].items():
        rec = dict(coords)
        rec["id"] = eid
        for e in params.get("entities", []):
            if e.get("id") == eid:
                rec["construction"] = bool(e.get("construction", False))
                break
        entities.append(rec)
    # Remember the dragged coords so commit can persist them (transient; no warm-state churn).
    st.last_entities = dict(res["entities"])
    st.last_statuses = dict(res.get("statuses", {}))
    # Drag is a transient soft-target preview; the solver's drag() carries no conflict signal, so a
    # drag never triggers the freeze path. Surface solve_rejected=False for response-shape parity
    # with sketch_solve/sketch_enter (C50f).
    return {"entities": entities, "statuses": res.get("statuses", {}),
            "solve_rejected": False,
            "constraints": _sketch_constraints(feat, res["entities"])}


def sketch_drag_commit(session: DocSession, sketch_fid: str,
                       emit: EmitFn | None = None) -> dict:
    """Persist the last dragged/solved coords as ONE undoable sketch_edit_entity batch.

    Builds a sketch_edit_entity op per entity from the last transient drag result and routes it
    through the normal apply_ops path (revision bump + rebuild + undo snapshot).
    """
    feat = _find_sketch_feature(session, sketch_fid)
    st = session.active_sketch
    coords = st.last_entities if (st and st.sketch_fid == sketch_fid) else None
    if not coords:
        # Nothing dragged — re-solve and persist solved positions (idempotent commit).
        packed = _solve_active(session, feat, warm=(st.solve_state if st else None))
        coords = packed["entity_map"]

    ops = []
    for eid, c in coords.items():
        patch = {k: v for k, v in c.items() if k != "type"}
        ops.append({"op": "sketch_edit_entity", "sketch": sketch_fid, "id": eid, "params": patch})

    resp = apply_ops(session, None, ops, emit=emit)
    # Re-solve so warm state matches the freshly persisted document, and surface the constraint
    # annotations (with refreshed `measured`) for the C++ sketcher (C35-overhaul §2).
    if session.active_sketch is not None and session.active_sketch.sketch_fid == sketch_fid:
        try:
            solved = sketch_solve(session, sketch_fid)
            resp["rebuild"]["doc_status"] = _doc_status(session, session.last_result)
            resp["constraints"] = solved.get("constraints", [])
        except Exception:  # noqa: BLE001
            pass
    if "constraints" not in resp:
        # Fall back to measuring against the just-committed (stored) coords.
        feat2 = _find_sketch_feature(session, sketch_fid)
        resp["constraints"] = _sketch_constraints(
            feat2, session.active_sketch.last_entities if session.active_sketch else {})
    return resp


def _persist_solved_coords(session: DocSession, feat: dict, coords: dict) -> bool:
    """Write solved entity coords into the document IN PLACE if they differ from stored. Returns
    True if anything changed (so the caller bumps the revision). No undo snapshot of its own —
    used by sketch_exit, which folds the persist into its rebuild as the last-solved positions
    (doc_schema.md §3.4)."""
    if not coords:
        return False
    changed = False
    for ent in feat.get("params", {}).get("entities", []):
        c = coords.get(ent["id"])
        if not c:
            continue
        for k, v in c.items():
            if k == "type":
                continue
            if ent.get(k) != v:
                ent[k] = v
                changed = True
    return changed


def sketch_exit(session: DocSession, sketch_fid: str, emit: EmitFn | None = None) -> dict:
    """Leave sketch-edit mode: persist last-solved coords, clear state, rebuild downstream.

    The document stores the *last solved* entity positions (doc_schema.md §3.4); a dim edit while
    in the sketch re-solves but doesn't itself rewrite stored coords, so exit bakes the active
    solve's coords in (as one undoable batch with the rebuild) before leaving."""
    st = session.active_sketch
    feat = _find_sketch_feature(session, sketch_fid)
    if st is not None and st.sketch_fid == sketch_fid and st.last_entities:
        session._push_undo()
        if _persist_solved_coords(session, feat, st.last_entities):
            session.revision += 1
            session.dirty = True
        else:
            session.undo_stack.pop()  # nothing changed — don't leave a no-op undo step
    session.active_sketch = None
    return rebuild(session, emit=emit)


# =========================================================================== C50 sketch projection
#
# sketch_project_ref — READ-ONLY projection of a datum plane / planar face / model edge into a
# sketch's (u,v) plane (the SolidWorks "project into sketch"). Projection is along the sketch
# plane normal. Never mutates the doc, never bumps the revision, never re-tessellates: the HOST
# takes the returned 2D entity and adds it itself (construction geometry + a whole-entity fix).


def _plane_uv_frame(plane):
    """(origin, u, v, n) of a build123d Plane as float64 world-coord arrays."""
    import numpy as np
    O = np.asarray(plane.origin.to_tuple(), dtype=float)
    u = np.asarray(plane.x_dir.to_tuple(), dtype=float)
    v = np.asarray(plane.y_dir.to_tuple(), dtype=float)
    n = np.asarray(plane.z_dir.to_tuple(), dtype=float)
    return O, u, v, n


def _plane_plane_line_uv(O1, u, v, n1, O2, n2):
    """Intersection line of a second plane (O2, n2) with the sketch plane (O1, n1), in
    sketch (u,v) coords. Returns ``(p_near, t)``: the line point nearest the sketch
    origin and the unit line direction (2-vectors). Raises ``unsupported_ref`` when
    the planes are parallel (|n1 × n2| < 1e-9)."""
    import numpy as np
    n2 = np.asarray(n2, dtype=float)
    n2 = n2 / max(float(np.linalg.norm(n2)), 1e-30)
    d3 = np.cross(n1, n2)
    L = float(np.linalg.norm(d3))
    if L < 1e-9:
        raise OpError("unsupported_ref", "plane is parallel to the sketch plane",
                      {"reason": "parallel planes"})
    # Point on both planes: P0 = a·n1 + b·n2 with n1·P0 = c1, n2·P0 = c2.
    c1 = float(n1 @ O1)
    c2 = float(n2 @ O2)
    k = float(n1 @ n2)
    denom = 1.0 - k * k          # == |n1 × n2|² for unit normals
    P0 = ((c1 - c2 * k) * n1 + (c2 - c1 * k) * n2) / denom
    p0 = np.array([float((P0 - O1) @ u), float((P0 - O1) @ v)])
    t3 = d3 / L                  # lies in BOTH planes, so its uv image is exact
    t = np.array([float(t3 @ u), float(t3 @ v)])
    t = t / max(float(np.linalg.norm(t)), 1e-30)
    p_near = p0 - float(p0 @ t) * t
    return p_near, t


def _dedupe_topo_infos(infos: list) -> list:
    """Drop duplicate TopoInfo entries (TopExp_Explorer revisits shared edges once per
    owning face). Identical duplicates would tie selectors.resolve's ambiguity margin
    at 0 and falsely dangle every shared-edge selector."""
    seen: set = set()
    out = []
    for info in infos:
        key = (info.surf,
               tuple(round(float(c), 6) for c in info.centroid),
               round(float(info.measure), 6) if info.measure is not None else None)
        if key in seen:
            continue
        seen.add(key)
        out.append(info)
    return out


def _resolve_topo_selector(result: CompileResult, kind: str, selector):
    """Resolve a face/edge selector against the CURRENT compiled solid bodies.

    Mirrors resolve_pick's body iteration (describe_faces/describe_edges over each
    solid body + selectors.resolve): every body is scored and the best accepted match
    wins. Returns ``(body_ordinal, body_out, info)``; the concrete subshape is then
    matched geometrically with :func:`_match_subshape`."""
    if not isinstance(selector, dict):
        raise OpError("invalid_op", "sketch_project_ref: 'selector' must be an object",
                      {"reason": "bad selector"})
    bodies = _solid_bodies(result)
    best = None
    last_reason = "no solid bodies"
    for ordinal, bo in enumerate(bodies, start=1):
        raw = bo.shape.wrapped
        infos = sel.describe_faces(raw) if kind == "face" else sel.describe_edges(raw)
        infos = _dedupe_topo_infos(infos)
        diag = _bbox_diag(bo.shape)
        rr = sel.resolve(selector, infos, diag)
        info = None
        score = rr.score
        if rr.index is not None:
            # resolve returns the candidate's TopoInfo.index — map it back to the
            # deduped list position.
            info = next(i for i in infos if i.index == rr.index)
        elif rr.dangling_reason and "margin" in rr.dangling_reason and rr.score < 0.01:
            # Same fallback as features/sketch._resolve_plane_ref: a near-zero best
            # score means the selector matches this exact element — accept it even
            # when a symmetric sibling sits inside the ambiguity margin.
            from engine.doc.selectors import _score_candidate
            hint = selector.get("hint_index") or 0
            scored = sorted(
                ((_score_candidate(selector, c, diag), abs(c.hint_index - hint), c)
                 for c in infos if c.kind == kind),
                key=lambda t: (t[0], t[1]))
            if scored and scored[0][0] < 0.01:
                score, _, info = scored[0]
        if info is not None:
            if best is None or score < best[0]:
                best = (score, ordinal, bo, info)
        elif rr.dangling_reason:
            last_reason = rr.dangling_reason
    if best is None:
        raise OpError("invalid_op",
                      f"sketch_project_ref: {kind} selector dangling — {last_reason}",
                      {"reason": last_reason})
    return best[1], best[2], best[3]


def _match_subshape(shapes, info):
    """The face/edge in *shapes* (build123d ``.faces()``/``.edges()``) geometrically
    matching a TopoInfo: nearest centroid, with the measure as tiebreak."""
    best, best_d = None, float("inf")
    for s in shapes:
        c = s.center()
        d = ((c.X - float(info.centroid[0])) ** 2
             + (c.Y - float(info.centroid[1])) ** 2
             + (c.Z - float(info.centroid[2])) ** 2)
        if info.measure is not None:
            try:
                m = s.area if hasattr(s, "area") and info.kind == "face" else s.length
                d += (float(m) - float(info.measure)) ** 2 * 1e-6
            except Exception:  # noqa: BLE001
                pass
        if d < best_d:
            best, best_d = s, d
    if best is None:
        raise OpError("invalid_op", "sketch_project_ref: resolved topology vanished",
                      {"reason": "no subshapes"})
    return best


def sketch_project_ref(session: DocSession, sketch_fid: str, ref) -> dict:
    """Project a reference into a sketch's (u,v) plane (worker_rpc sketch_project_ref, C50).

    ``ref`` is one of::

        {"datum": "front"|"top"|"right"}                 (also "plane.front" etc.)
        {"kind": "face"|"edge", "selector": {...}}        (selector from resolve_pick)

    Returns ``{"entity": E, "source": str}`` with E a plain 2D entity dict in the
    sketch's (u,v) coords: ``line {p1,p2}`` | ``circle {center,radius}`` | ``point {p}``.

    Math: planes (datum / planar face) intersect the sketch plane in a line; edges are
    projected along the sketch normal (linear edge -> line, or point when the projected
    endpoints coincide; circular edge with axis ∥ sketch normal -> circle). Anything
    else raises ``OpError("unsupported_ref", ...)``. READ-ONLY (no doc mutation, no
    revision bump, no mesh rebuild)."""
    import math
    import numpy as np

    feat = _find_sketch_feature(session, sketch_fid)
    result = _ensure_built(session)
    plane = _resolve_sketch_plane(session, feat, result)
    O1, u, v, n1 = _plane_uv_frame(plane)

    if not isinstance(ref, dict) or ("datum" not in ref and "kind" not in ref):
        raise OpError("invalid_op",
                      "sketch_project_ref: 'ref' must carry 'datum' or 'kind'+'selector'",
                      {"reason": "bad ref"})

    def _uv(P3):
        d = np.asarray(P3, dtype=float) - O1
        return [float(d @ u), float(d @ v)]

    # ---- datum plane -> intersection line --------------------------------------
    if "datum" in ref:
        from engine.doc.features.datum import reserved_datums
        name = str(ref["datum"])
        if not name.startswith("plane."):
            name = "plane." + name
        d = reserved_datums().get(name)
        if d is None or d.kind != "plane":
            raise OpError("invalid_op",
                          f"sketch_project_ref: unknown datum '{ref['datum']}'",
                          {"reason": "unknown datum"})
        O2, _, _, n2 = _plane_uv_frame(d.obj)
        p_near, t = _plane_plane_line_uv(O1, u, v, n1, O2, n2)
        # Span: half-length max(2 × model bbox diagonal, 100 mm), centered on the
        # line point nearest the sketch origin.
        diag = 0.0
        for bo in _solid_bodies(result):
            diag = max(diag, _bbox_diag(bo.shape))
        half = max(2.0 * diag, 100.0)
        p1 = p_near - half * t
        p2 = p_near + half * t
        return {"entity": {"type": "line",
                           "p1": [float(p1[0]), float(p1[1])],
                           "p2": [float(p2[0]), float(p2[1])]},
                "source": name}

    kind = ref.get("kind")

    # ---- planar face -> intersection line, spanned by the face's vertices ------
    if kind == "face":
        ordinal, bo, info = _resolve_topo_selector(result, "face", ref.get("selector"))
        if info.surf != "plane" or info.axis is None:
            raise OpError("unsupported_ref",
                          "sketch_project_ref: only planar faces can be projected",
                          {"reason": f"face surf '{info.surf}'"})
        p_near, t = _plane_plane_line_uv(O1, u, v, n1,
                                         np.asarray(info.centroid, dtype=float),
                                         np.asarray(info.axis, dtype=float))
        face = _match_subshape(bo.shape.faces(), info)
        # Face vertices projected onto the line direction -> min/max span, +5 mm pad.
        ss = []
        for vx in face.vertices():
            duv = np.asarray(_uv(vx.center().to_tuple()), dtype=float)
            ss.append(float((duv - p_near) @ t))
        if not ss:
            ss = [0.0]
        lo, hi = min(ss) - 5.0, max(ss) + 5.0
        p1 = p_near + lo * t
        p2 = p_near + hi * t
        return {"entity": {"type": "line",
                           "p1": [float(p1[0]), float(p1[1])],
                           "p2": [float(p2[0]), float(p2[1])]},
                "source": f"planar face (body {ordinal})"}

    # ---- edge -> line / point / circle ------------------------------------------
    if kind == "edge":
        ordinal, bo, info = _resolve_topo_selector(result, "edge", ref.get("selector"))
        edge = _match_subshape(bo.shape.edges(), info)
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Line, GeomAbs_Circle
        ad = BRepAdaptor_Curve(edge.wrapped)
        ct = ad.GetType()
        if ct == GeomAbs_Line:
            Pa = ad.Value(ad.FirstParameter())
            Pb = ad.Value(ad.LastParameter())
            a = _uv([Pa.X(), Pa.Y(), Pa.Z()])
            b = _uv([Pb.X(), Pb.Y(), Pb.Z()])
            if math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-6:
                # Edge runs along the sketch normal — its projection is one point.
                return {"entity": {"type": "point", "p": a},
                        "source": f"linear edge (body {ordinal}, projects to a point)"}
            return {"entity": {"type": "line", "p1": a, "p2": b},
                    "source": f"linear edge (body {ordinal})"}
        if ct == GeomAbs_Circle:
            circ = ad.Circle()
            axd = circ.Axis().Direction()
            ax3 = np.array([axd.X(), axd.Y(), axd.Z()], dtype=float)
            ax3 = ax3 / max(float(np.linalg.norm(ax3)), 1e-30)
            if abs(float(ax3 @ n1)) <= 1.0 - 1e-6:
                raise OpError("unsupported_ref",
                              "edge type not supported for sketch projection",
                              {"reason": "circle axis not parallel to the sketch normal"})
            loc = circ.Location()
            return {"entity": {"type": "circle",
                               "center": _uv([loc.X(), loc.Y(), loc.Z()]),
                               "radius": float(circ.Radius())},
                    "source": f"circular edge (body {ordinal})"}
        raise OpError("unsupported_ref", "edge type not supported for sketch projection",
                      {"reason": f"curve type '{info.surf}'"})

    raise OpError("invalid_op",
                  f"sketch_project_ref: ref kind '{kind}' must be face|edge",
                  {"reason": "bad kind"})
