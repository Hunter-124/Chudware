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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from engine import paths
from engine.doc.compiler import CompileCache, CompileResult, compile_document
from engine.doc.ops import OpError, apply_ops as _apply_ops_to_doc
from engine.doc import selectors as sel
from engine.doc import tessellate as tess

# Make sure feature compilers are registered (compile_document needs them).
import engine.doc.features  # noqa: F401

EmitFn = Callable[[dict], None]

_UNDO_LIMIT = 100
_PROGRESS_MS = 300.0

# Default body colour (matches the host's grey, doc_ops.md example).
_DEFAULT_COLOR = (0.71, 0.76, 0.82)


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

    result = compile_document(session.doc, session.compile_cache)
    maybe_progress("compiled")
    session.last_result = result
    return result


def _write_aicm(session: DocSession, result: CompileResult) -> tuple[str, list[dict]]:
    """Tessellate every solid body and write the AICM; return (aicm_path, bodies-list)."""
    body_tesses: list[tess.BodyTess] = []
    bodies_meta: list[dict] = []
    for i, bo in enumerate(_solid_bodies(result), start=1):
        color = tuple(bo.color) if bo.color else _DEFAULT_COLOR
        bt = tess.tessellate_body(bo.shape, body_id=i, name=bo.name or f"Body{i}", color=color)
        body_tesses.append(bt)
        bodies_meta.append({"body_id": i, "name": bo.name or f"Body{i}", "color": list(color)})

    aicm = _aicm_path(session.doc_id, session.revision)
    tess.write_aicm_file(aicm, body_tesses)
    return str(aicm), bodies_meta


def _tree(session: DocSession) -> dict:
    return copy.deepcopy(session.doc)


def _doc_status(session: DocSession, result: CompileResult) -> dict:
    return {"active_sketch": None, "mates": None}


def _ops_response(session: DocSession, result: CompileResult,
                  id_map: dict[str, str]) -> dict:
    aicm_path, bodies_meta = _write_aicm(session, result)
    feature_status = [
        {"id": s["id"], "ok": s["ok"], "ms": s["ms"], "error": s["error"],
         "dangling_refs": s.get("dangling_refs", [])}
        for s in result.feature_status
    ]
    return {
        "revision": session.revision,
        "id_map": id_map,
        "tree": _tree(session),
        "rebuild": {
            "feature_status": feature_status,
            "doc_status": _doc_status(session, result),
        },
        "tess": {"aicm_path": aicm_path, "bodies": bodies_meta},
    }


# --------------------------------------------------------------------------- mutate


def apply_ops(session: DocSession, base_revision: int | None, ops: list[dict],
              emit: EmitFn | None = None) -> dict:
    """Apply an op batch, bump revision, rebuild, and return the ops-response (doc_ops.md §1).

    ``base_revision`` (optional optimistic-concurrency guard): if provided and it does not match
    the session's current revision, raises ``stale_revision``.
    """
    if base_revision is not None and base_revision != session.revision:
        raise OpError("stale_revision",
                      f"base_revision {base_revision} != current {session.revision}",
                      {"current_revision": session.revision})

    new_doc, id_map = _apply_ops_to_doc(session.doc, ops)  # raises OpError on any failure

    # Commit: snapshot the OLD doc for undo, swap in the new doc, bump revision.
    session._push_undo()
    session.doc = new_doc
    session.revision += 1
    session.dirty = True

    result = _rebuild(session, emit)
    return _ops_response(session, result, id_map)


def rebuild(session: DocSession, emit: EmitFn | None = None) -> dict:
    """Forced rebuild (no op application, no revision bump)."""
    result = _rebuild(session, emit)
    return _ops_response(session, result, {})


def tree(session: DocSession) -> dict:
    return {"revision": session.revision, "tree": _tree(session)}


def undo(session: DocSession, emit: EmitFn | None = None) -> dict:
    if not session.undo_stack:
        raise OpError("invalid_op", "nothing to undo", {"reason": "empty undo stack"})
    import json
    session.redo_stack.append(session._snapshot())
    snap = session.undo_stack.pop()
    session.doc = json.loads(snap)
    session.revision += 1
    session.dirty = True
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
    result = _rebuild(session, emit)
    return _ops_response(session, result, {})


# --------------------------------------------------------------------------- resolve pick


def _ensure_built(session: DocSession) -> CompileResult:
    if session.last_result is None:
        _rebuild(session)
    assert session.last_result is not None
    return session.last_result


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


def _body_owner(result: CompileResult, bo) -> str:
    """A representative owner feature for a whole-body selector: the most common face owner."""
    owners = result.face_owners.get(bo.body_id, {})
    if not owners:
        return ""
    counts: dict[str, int] = {}
    for v in owners.values():
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)
