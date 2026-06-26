"""ECAD document sessions — live state + compile-on-apply + the ops-response (ecad_rpc.md §1/§3).

An :class:`EcadSession` holds one open ECAD document (schematic OR board), its revision counter,
bounded undo/redo stacks of full document-JSON snapshots, and the last compile result.
:func:`apply_ops` runs the op batch (engine.ecad.ops), bumps the revision, computes ``compile``
(best-effort netlist/ERC for sch, DRC/ratsnest for pcb), and returns the ops-response dict the
worker / MCP bridge hands to the host.

The compile sub-modules (netlist / erc / drc) are built in PARALLEL with this one, so every compile
call is a LAZY import inside a try/except: a missing module simply leaves that compile sub-field
null/[]. A document with ERC/DRC violations is still a valid edited document (ecad_ops.md §2.3) — a
compile failure never rejects the ops.

Mirrors the structure & undo discipline of :mod:`engine.doc.session` (bounded JSON-snapshot stacks,
``stale_revision`` guard, dataclass + module registry).
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from engine.ecad import autosave as _autosave
from engine.ecad import schema
from engine.ecad.ops import OpError, apply_ops as _apply_ops_to_doc

EmitFn = Callable[[dict], None]

_UNDO_LIMIT = 100

# Interactive compile uses a COARSER zone-fill grid than the full-resolution export path
# (engine.ecad.zonefill.DEFAULT_RESOLUTION = 0.1mm). 0.25mm is ~6x cheaper (cost is ~1/res^2)
# while still giving a representative pour preview after every rebuild; full-resolution fill
# happens on explicit ecad_fill_zones / export (see rpc._ecad_fill_zones). A board with no zones
# is never touched (the fill block is guarded by zone presence below).
_INTERACTIVE_FILL_RESOLUTION = 0.25


# --------------------------------------------------------------------------- session


@dataclass
class EcadSession:
    doc_id: str
    doc: dict
    revision: int = 0
    path: Optional[str] = None
    dirty: bool = False
    undo_stack: list[str] = field(default_factory=list)   # JSON snapshots (doc only)
    redo_stack: list[str] = field(default_factory=list)
    last_compile: Optional[dict] = None

    @property
    def kind(self) -> str:
        return schema.doc_kind(self.doc)

    # -- snapshot helpers -------------------------------------------------

    def _snapshot(self) -> str:
        return json.dumps(self.doc, sort_keys=True, separators=(",", ":"))

    def _push_undo(self) -> None:
        self.undo_stack.append(self._snapshot())
        if len(self.undo_stack) > _UNDO_LIMIT:
            self.undo_stack.pop(0)
        self.redo_stack.clear()


# --------------------------------------------------------------------------- registry


_REGISTRY: dict[str, EcadSession] = {}


def get_session(doc_id: str) -> EcadSession:
    s = _REGISTRY.get(doc_id)
    if s is None:
        raise OpError("unknown_doc", f"no open ECAD document '{doc_id}'", {"doc_id": doc_id})
    return s


def close_session(doc_id: str) -> bool:
    removed = _REGISTRY.pop(doc_id, None) is not None
    if removed:
        # Drop the autosave too: a deliberately-closed doc must NOT be resurrected on restart.
        _autosave.remove(doc_id)
    return removed


def list_sessions() -> list[dict]:
    return [{
        "doc_id": s.doc_id, "name": s.doc.get("name", s.doc_id),
        "kind": s.kind, "path": s.path, "revision": s.revision, "dirty": s.dirty,
    } for s in _REGISTRY.values()]


def iter_sessions() -> list[EcadSession]:
    """Every open session (the live EcadSession objects, not summaries) — used by the MCP layer to
    autosave all open ECAD docs before a heavy op (mirrors cad_tools._autosave_all_open)."""
    return list(_REGISTRY.values())


def _register(session: EcadSession) -> None:
    _REGISTRY[session.doc_id] = session


# --------------------------------------------------------------------------- new / open / close


def new_session(kind: str = "sch", name: str | None = None) -> EcadSession:
    """Create + register a fresh session for a new ``sch`` or ``pcb`` document."""
    if kind == "sch":
        doc = schema.new_schematic(name or "Schematic1")
    elif kind == "pcb":
        doc = schema.new_board(name or "Board1")
    else:
        raise OpError("invalid_op", f"doc kind must be sch|pcb, got '{kind}'",
                      {"reason": "bad kind"})
    doc_id = uuid.uuid4().hex[:12]
    session = EcadSession(doc_id=doc_id, doc=doc, revision=0)
    _register(session)
    _autosave.autosave_session(session)  # crash-recovery: persist from creation
    return session


def open_session(doc: dict, *, path: str | None = None) -> EcadSession:
    """Register a session around an already-parsed ECAD document (used by ecad_open)."""
    if not (schema.is_schematic(doc) or schema.is_board(doc)):
        raise OpError("invalid_op", f"not an ECAD document ({doc.get('schema')!r})",
                      {"reason": "unknown schema"})
    doc_id = uuid.uuid4().hex[:12]
    session = EcadSession(doc_id=doc_id, doc=copy.deepcopy(doc), revision=0, path=path)
    _register(session)
    _autosave.autosave_session(session)  # crash-recovery: persist from open
    return session


# --------------------------------------------------------------------------- compile (best-effort)


def _fill_zones_into(doc: dict, resolution: float) -> int | None:
    """Recompute every copper zone's ``filled_polys`` IN PLACE on ``doc``, returning the number of
    zones filled (or ``None`` if there were no zones / fill was unavailable).

    GUARD: a board with no zones returns immediately (no work, no cost) — zone-less boards are
    never slowed. ``engine.ecad.zonefill`` is a LAZY import wrapped in try/except so a missing
    module (or a fill failure) degrades gracefully and never rejects a rebuild. ``fill_zones`` is
    pure (returns a filled deep copy); we copy each zone's freshly-computed ``filled_polys`` back
    onto the working doc's matching zone so fab/gerber.py and ecad_describe see current fills.
    """
    zones = doc.get("zones")
    if not zones:
        return None
    try:
        from engine.ecad import zonefill as zonefill_mod  # type: ignore
    except ImportError:
        return None
    try:
        filled = zonefill_mod.fill_zones(doc, resolution=resolution)
    except Exception:  # noqa: BLE001 - a fill failure must not reject the rebuild
        return None
    filled_by_id = {z.get("id"): z.get("filled_polys", []) for z in filled.get("zones", [])}
    n = 0
    for zone in zones:
        zone["filled_polys"] = filled_by_id.get(zone.get("id"), [])
        n += 1
    return n


def _compile(doc: dict) -> dict:
    """Compute the ``compile`` field (ecad_rpc.md §1). Best-effort via LAZY imports.

    sch -> ``netlist`` (engine.ecad.netlist.build_netlist) + ``erc`` (engine.ecad.erc.run_erc).
    pcb -> ``drc`` (engine.ecad.drc.run_drc) + ``ratsnest``.
    Each is wrapped in try/except: a missing parallel module leaves that sub-field null/[]. Never
    raises (a compile failure must not reject ops — ecad_ops.md §2.3)."""
    t0 = time.perf_counter()
    out: dict[str, Any] = {"netlist": None, "erc": [], "drc": [], "ratsnest": [], "stats": {}}

    if schema.is_schematic(doc):
        netlist = None
        try:
            from engine.ecad import netlist as netlist_mod  # type: ignore
            netlist = netlist_mod.build_netlist(doc)
            out["netlist"] = netlist
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 - compile never rejects ops
            out["stats"]["netlist_error"] = str(exc)
        try:
            from engine.ecad import erc as erc_mod  # type: ignore
            out["erc"] = erc_mod.run_erc(doc, netlist) if netlist is not None \
                else erc_mod.run_erc(doc)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            out["stats"]["erc_error"] = str(exc)
        out["stats"]["nets"] = len(netlist.get("nets", {})) if isinstance(netlist, dict) else 0
    elif schema.is_board(doc):
        # Refresh copper-pour fills so each zone's filled_polys is current after the rebuild
        # (fab/gerber.py reads zone["filled_polys"] directly). GUARDED by zone presence so a
        # zone-less board pays nothing, and run at a COARSER resolution than export to keep the
        # interactive path fast (full-resolution fill is explicit — see rpc._ecad_fill_zones).
        zfilled = _fill_zones_into(doc, _INTERACTIVE_FILL_RESOLUTION)
        if zfilled is not None:
            out["stats"]["zones_filled"] = zfilled
        try:
            from engine.ecad import drc as drc_mod  # type: ignore
            result = drc_mod.run_drc(doc)
            # drc may return a list of violations, or {violations, ratsnest}
            if isinstance(result, dict):
                out["drc"] = result.get("violations", result.get("drc", []))
                out["ratsnest"] = result.get("ratsnest", [])
            else:
                out["drc"] = result or []
                # run_drc returns a flat violation list; the airwires live in a separate entry
                # point, so surface them in compile.ratsnest (the ops-response contract field).
                if hasattr(drc_mod, "ratsnest"):
                    out["ratsnest"] = drc_mod.ratsnest(doc)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            out["stats"]["drc_error"] = str(exc)
        out["stats"]["nets"] = len(doc.get("nets", []))
        out["stats"]["unrouted"] = len(out["ratsnest"])

    out["stats"]["ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return out


# --------------------------------------------------------------------------- ops-response


def _ops_response(session: EcadSession, id_map: dict[str, str],
                  warnings: list[str] | None = None) -> dict:
    compile_field = _compile(session.doc)
    session.last_compile = compile_field
    resp = {
        "revision": session.revision,
        "id_map": id_map,
        "kind": session.kind,
        "tree": copy.deepcopy(session.doc),
        "compile": compile_field,
    }
    if warnings:
        resp["warnings"] = warnings
    return resp


# --------------------------------------------------------------------------- mutate


def apply_ops(session: EcadSession, base_revision: int | None, ops: list[dict],
              emit: EmitFn | None = None) -> dict:
    """Apply an op batch, bump the revision, compile, and return the ops-response (ecad_rpc.md §1).

    ``base_revision`` (optimistic-concurrency guard): if provided and it does not match the
    session's current revision, returns a ``stale_revision`` error response (the caller resyncs).

    The op batch is applied atomically by :func:`engine.ecad.ops.apply_ops` (validate-all-then-swap)
    BEFORE any state changes — so a bad batch leaves the session's revision/undo untouched. On
    success we push an undo snapshot of the OLD doc, swap in the new doc, bump the revision by 1,
    compile (best-effort), and return the full response.
    """
    if base_revision is not None and base_revision != session.revision:
        raise OpError("stale_revision",
                      f"base_revision {base_revision} != current {session.revision}",
                      {"current_revision": session.revision})

    res = _apply_ops_to_doc(session.doc, ops)  # raises OpError on any failure (no state change yet)
    new_doc = res["doc"]

    session._push_undo()
    session.doc = new_doc
    session.revision += 1
    session.dirty = True

    resp = _ops_response(session, res["id_map"], res.get("warnings"))
    _autosave.autosave_session(session)  # crash-recovery: persist after every mutation
    return resp


def swap_doc(session: EcadSession, new_doc: dict, *, base_revision: int | None = None,
             warnings: list[str] | None = None, emit: EmitFn | None = None) -> dict:
    """Swap a wholesale-replaced document into *session* (one revision bump), like :func:`apply_ops`.

    Used by cross-document operations (e.g. ``ecad_update_from_sch`` back-annotation) that compute a
    new document outside the single-doc op vocabulary. The new doc is validated first (a schema
    failure raises :class:`OpError` and leaves the session untouched — same atomicity as apply_ops):
    on success we push an undo snapshot of the OLD doc, swap in the new doc, bump the revision by 1,
    compile, and return the full ops-response (with an empty ``id_map``).
    """
    if base_revision is not None and base_revision != session.revision:
        raise OpError("stale_revision",
                      f"base_revision {base_revision} != current {session.revision}",
                      {"current_revision": session.revision})

    errs = schema.validate(new_doc)
    if errs:
        raise OpError("invalid_op",
                      "resulting document failed validation: " + "; ".join(errs[:5]),
                      {"reason": errs[0] if errs else "invalid document", "all_errors": errs})

    session._push_undo()
    session.doc = copy.deepcopy(new_doc)
    session.revision += 1
    session.dirty = True

    resp = _ops_response(session, {}, warnings)
    _autosave.autosave_session(session)  # crash-recovery: persist after every mutation
    return resp


def rebuild(session: EcadSession, emit: EmitFn | None = None) -> dict:
    """Force a recompute of netlist/ERC/DRC (ecad_rpc.md ecad_rebuild). No op application, no
    revision bump."""
    return _ops_response(session, {})


def tree(session: EcadSession) -> dict:
    """Cached tree + compile, no rebuild (ecad_rpc.md ecad_tree)."""
    compile_field = session.last_compile if session.last_compile is not None else _compile(session.doc)
    session.last_compile = compile_field
    return {"revision": session.revision, "kind": session.kind,
            "tree": copy.deepcopy(session.doc), "compile": compile_field}


# --------------------------------------------------------------------------- undo / redo


def undo(session: EcadSession, emit: EmitFn | None = None) -> dict:
    if not session.undo_stack:
        raise OpError("invalid_op", "nothing to undo", {"reason": "empty undo stack"})
    session.redo_stack.append(session._snapshot())
    snap = session.undo_stack.pop()
    session.doc = json.loads(snap)
    session.revision += 1
    session.dirty = True
    resp = _ops_response(session, {})
    _autosave.autosave_session(session)  # crash-recovery: persist after every mutation
    return resp


def redo(session: EcadSession, emit: EmitFn | None = None) -> dict:
    if not session.redo_stack:
        raise OpError("invalid_op", "nothing to redo", {"reason": "empty redo stack"})
    session.undo_stack.append(session._snapshot())
    if len(session.undo_stack) > _UNDO_LIMIT:
        session.undo_stack.pop(0)
    snap = session.redo_stack.pop()
    session.doc = json.loads(snap)
    session.revision += 1
    session.dirty = True
    resp = _ops_response(session, {})
    _autosave.autosave_session(session)  # crash-recovery: persist after every mutation
    return resp
