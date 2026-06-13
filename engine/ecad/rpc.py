"""ECAD worker-RPC handlers — registered into engine/worker.py via a fenced append.

Exposes:
    METHODS                  dict[str, handler]  — merged into worker._METHODS
    STRUCTURED_ERROR_METHODS set[str]            — merged into worker._STRUCTURED_ERROR_METHODS

Every handler signature: ``def _ecad_x(params: dict, emit: EmitFn) -> dict``

Lazy-imports heavy / optional sub-modules (fab, sim, threed, formats) so a missing module
never breaks worker startup.  Optional modules that are absent return a structured
``not_available`` error rather than crashing.
"""
from __future__ import annotations

from typing import Callable

from engine.ecad.ops import OpError

EmitFn = Callable[[dict], None]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _require(params: dict, key: str, label: str = "") -> object:
    v = params.get(key)
    if v is None:
        raise OpError("invalid_op",
                      f"ecad rpc: missing required param '{key}'" + (f" ({label})" if label else ""),
                      {"reason": f"missing {key}"})
    return v


def _not_available(feature: str) -> dict:
    """Return a structured 'not available yet' error dict (not an exception) for optional modules."""
    raise OpError("not_available",
                  f"{feature} is not available in this build",
                  {"reason": "module not present", "feature": feature})


# ---------------------------------------------------------------------------
# ecad_new  {kind, name?} -> ops-response + doc_id
# ---------------------------------------------------------------------------

def _ecad_new(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    kind = params.get("kind", "sch")
    name = params.get("name")
    s = esess.new_session(kind=kind, name=name)
    resp = esess.rebuild(s)
    resp["doc_id"] = s.doc_id
    return resp


# ---------------------------------------------------------------------------
# ecad_open  {path} -> ops-response + doc_id + warnings
# ---------------------------------------------------------------------------

def _ecad_open(params: dict, _emit: EmitFn) -> dict:
    path = str(_require(params, "path"))
    import json, pathlib
    from engine.ecad import session as esess, schema

    p = pathlib.Path(path)
    warnings: list[str] = []

    # Try engine.ecad.formats (the .ecad+ container) if it exists, else fall back to plain JSON.
    doc: dict | None = None
    try:
        from engine.ecad import formats  # type: ignore
        doc, extra_warnings = formats.open(path)
        warnings.extend(extra_warnings or [])
    except ImportError:
        pass  # formats module not yet built — use plain JSON below
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"ecad.formats.open failed ({exc}); trying plain JSON")

    if doc is None:
        # Plain JSON fallback (also handles .kicad_sch / .kicad_pcb stubs in the future)
        try:
            raw = p.read_text(encoding="utf-8")
            doc = json.loads(raw)
        except Exception as exc:
            raise OpError("io_error", f"cannot read '{path}': {exc}", {"path": path}) from exc

    s = esess.open_session(doc, path=path)
    resp = esess.rebuild(s)
    resp["doc_id"] = s.doc_id
    resp["warnings"] = warnings
    return resp


# ---------------------------------------------------------------------------
# ecad_save  {doc_id, path?} -> {path}
# ---------------------------------------------------------------------------

def _ecad_save(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    doc_id = str(_require(params, "doc_id"))
    s = esess.get_session(doc_id)
    path = params.get("path") or s.path
    if not path:
        raise OpError("io_error", "ecad_save: no path given and document has no current path",
                      {"reason": "missing path"})

    # Try engine.ecad.formats first; fall back to plain JSON.
    try:
        from engine.ecad import formats  # type: ignore
        out = formats.save(s.doc, path)
        s.path = out
        s.dirty = False
        return {"path": out}
    except ImportError:
        pass  # formats module not yet built
    except Exception as exc:  # noqa: BLE001
        raise OpError("io_error", f"ecad.formats.save failed: {exc}", {"path": path}) from exc

    # Plain JSON fallback
    import json, pathlib
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s.doc, indent=2, sort_keys=True), encoding="utf-8")
    s.path = str(p)
    s.dirty = False
    return {"path": str(p)}


# ---------------------------------------------------------------------------
# ecad_close  {doc_id} -> {closed: true}
# ---------------------------------------------------------------------------

def _ecad_close(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    doc_id = str(_require(params, "doc_id"))
    closed = esess.close_session(doc_id)
    return {"closed": closed}


# ---------------------------------------------------------------------------
# ecad_apply_ops  {doc_id, base_revision, ops} -> ops-response
# ---------------------------------------------------------------------------

def _ecad_apply_ops(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    doc_id = str(_require(params, "doc_id"))
    s = esess.get_session(doc_id)
    return esess.apply_ops(
        s,
        base_revision=params.get("base_revision"),
        ops=params.get("ops") or [],
    )


# ---------------------------------------------------------------------------
# ecad_rebuild  {doc_id} -> ops-response
# ---------------------------------------------------------------------------

def _ecad_rebuild(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    doc_id = str(_require(params, "doc_id"))
    s = esess.get_session(doc_id)
    return esess.rebuild(s)


# ---------------------------------------------------------------------------
# ecad_tree  {doc_id} -> {revision, kind, tree, compile}
# ---------------------------------------------------------------------------

def _ecad_tree(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    doc_id = str(_require(params, "doc_id"))
    s = esess.get_session(doc_id)
    return esess.tree(s)


# ---------------------------------------------------------------------------
# ecad_undo  {doc_id} -> ops-response
# ---------------------------------------------------------------------------

def _ecad_undo(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    doc_id = str(_require(params, "doc_id"))
    s = esess.get_session(doc_id)
    return esess.undo(s)


# ---------------------------------------------------------------------------
# ecad_redo  {doc_id} -> ops-response
# ---------------------------------------------------------------------------

def _ecad_redo(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess
    doc_id = str(_require(params, "doc_id"))
    s = esess.get_session(doc_id)
    return esess.redo(s)


# ---------------------------------------------------------------------------
# ecad_resolve_pick  {doc_id, layer?, xy, kinds?} -> {selector}
# ---------------------------------------------------------------------------

def _ecad_resolve_pick(params: dict, _emit: EmitFn) -> dict:
    """Canvas pick → typed ref (pin/pad/track/via/net).

    Best-effort stub: the full spatial index is built by the fab / renderer layer.
    Returns the closest matching object if engine.ecad.pick exists, else a minimal
    'unknown' selector so the caller always gets a dict back.
    """
    doc_id = str(_require(params, "doc_id"))
    xy = params.get("xy")
    kinds = params.get("kinds")
    layer = params.get("layer")

    try:
        from engine.ecad import pick as ecad_pick  # type: ignore
        from engine.ecad import session as esess
        s = esess.get_session(doc_id)
        return ecad_pick.resolve(s.doc, xy=xy, layer=layer, kinds=kinds)
    except ImportError:
        pass  # pick module not yet built — return a stub

    return {"selector": {"kind": "unknown", "xy": xy, "layer": layer}}


# ---------------------------------------------------------------------------
# ecad_describe  {doc_id, budget?} -> compact summary dict
# ---------------------------------------------------------------------------

def _ecad_describe(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess, schema
    doc_id = str(_require(params, "doc_id"))
    s = esess.get_session(doc_id)
    doc = s.doc

    # Delegate to a dedicated describe helper if available.
    try:
        from engine.ecad import describe as ecad_describe  # type: ignore
        return ecad_describe.describe(doc, budget=params.get("budget"))
    except ImportError:
        pass  # describe module not yet built — build a minimal summary

    # Minimal fallback summary.
    compile_info = s.last_compile or {}
    if schema.is_schematic(doc):
        return {
            "kind": "sch",
            "name": doc.get("name", ""),
            "revision": s.revision,
            "symbols": len(doc.get("symbols", [])),
            "wires": len(doc.get("wires", [])),
            "nets": compile_info.get("stats", {}).get("nets", 0),
            "erc_violations": len(compile_info.get("erc", [])),
        }
    else:
        return {
            "kind": "pcb",
            "name": doc.get("name", ""),
            "revision": s.revision,
            "footprints": len(doc.get("footprints", [])),
            "tracks": len(doc.get("tracks", [])),
            "vias": len(doc.get("vias", [])),
            "nets": compile_info.get("stats", {}).get("nets", 0),
            "unrouted": compile_info.get("stats", {}).get("unrouted", 0),
            "drc_violations": len(compile_info.get("drc", [])),
        }


# ---------------------------------------------------------------------------
# ecad_export  {doc_id, kind, path, opts?} -> {paths:[...]}
# ---------------------------------------------------------------------------

_EXPORT_KINDS = frozenset(
    {"gerber", "drill", "bom", "cpl", "spice", "netlist", "kicad"}
)


def _ecad_export(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess, schema
    doc_id = str(_require(params, "doc_id"))
    kind = str(_require(params, "kind"))
    out_path = str(_require(params, "path"))
    opts: dict = params.get("opts") or {}

    if kind not in _EXPORT_KINDS:
        raise OpError("invalid_op",
                      f"ecad_export: unknown kind '{kind}'; valid: {sorted(_EXPORT_KINDS)}",
                      {"reason": f"unknown export kind '{kind}'"})

    s = esess.get_session(doc_id)
    doc = s.doc

    import os, pathlib
    out_dir = str(pathlib.Path(out_path))

    # ---- gerber (pcb only) ---------------------------------------------------
    if kind == "gerber":
        if not schema.is_board(doc):
            raise OpError("invalid_op", "gerber export requires a pcb document",
                          {"reason": "wrong doc kind"})
        try:
            from engine.ecad.fab import gerber as gerber_mod
        except ImportError:
            _not_available("engine.ecad.fab.gerber")
        result = gerber_mod.write_gerbers(doc, out_dir)
        return {"paths": list(result.values())}

    # ---- drill / excellon (pcb only) ----------------------------------------
    if kind == "drill":
        if not schema.is_board(doc):
            raise OpError("invalid_op", "drill export requires a pcb document",
                          {"reason": "wrong doc kind"})
        try:
            from engine.ecad.fab import excellon as excellon_mod
        except ImportError:
            _not_available("engine.ecad.fab.excellon")
        result = excellon_mod.write_drill(doc, out_dir)
        return {"paths": list(result.values())}

    # ---- bom (pcb only) -----------------------------------------------------
    if kind == "bom":
        if not schema.is_board(doc):
            raise OpError("invalid_op", "bom export requires a pcb document",
                          {"reason": "wrong doc kind"})
        try:
            from engine.ecad.fab import bom as bom_mod
        except ImportError:
            _not_available("engine.ecad.fab.bom")
        fmt = opts.get("fmt", "csv")
        content = bom_mod.write_bom(doc, fmt=fmt,
                                    exclude_dnp=bool(opts.get("exclude_dnp", True)))
        p = pathlib.Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"paths": [str(p)]}

    # ---- cpl / placement (pcb only) -----------------------------------------
    if kind == "cpl":
        if not schema.is_board(doc):
            raise OpError("invalid_op", "cpl export requires a pcb document",
                          {"reason": "wrong doc kind"})
        try:
            from engine.ecad.fab import placement as placement_mod
        except ImportError:
            _not_available("engine.ecad.fab.placement")
        content = placement_mod.write_placement(
            doc,
            exclude_tht=bool(opts.get("exclude_tht", True)),
            exclude_virtual=bool(opts.get("exclude_virtual", True)),
        )
        p = pathlib.Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"paths": [str(p)]}

    # ---- spice (sch only) ---------------------------------------------------
    if kind == "spice":
        if not schema.is_schematic(doc):
            raise OpError("invalid_op", "spice export requires a sch document",
                          {"reason": "wrong doc kind"})
        try:
            from engine.ecad import netlist as netlist_mod
            from engine.sim import spice as spice_mod
        except ImportError as exc:
            _not_available(f"spice export (missing module: {exc})")
        nl = netlist_mod.build_netlist(doc)
        circuit, warnings = spice_mod.netlist_to_spice_circuit(
            nl, sim_params=opts.get("sim_params"))
        content = spice_mod.write_spice(circuit, study=opts.get("study"))
        p = pathlib.Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"paths": [str(p)], "warnings": warnings}

    # ---- netlist (sch only) -------------------------------------------------
    if kind == "netlist":
        if not schema.is_schematic(doc):
            raise OpError("invalid_op", "netlist export requires a sch document",
                          {"reason": "wrong doc kind"})
        try:
            from engine.ecad import netlist as netlist_mod
        except ImportError:
            _not_available("engine.ecad.netlist")
        import json
        nl = netlist_mod.build_netlist(doc)
        p = pathlib.Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(nl, indent=2), encoding="utf-8")
        return {"paths": [str(p)]}

    # ---- kicad (sch or pcb) -------------------------------------------------
    if kind == "kicad":
        try:
            from engine.ecad import kicad_export  # type: ignore
        except ImportError:
            _not_available("engine.ecad.kicad_export")
        result = kicad_export.export(doc, out_path, opts=opts)
        paths = result if isinstance(result, list) else [result]
        return {"paths": paths}

    # Should never reach here (guarded above), but be safe.
    raise OpError("invalid_op", f"unhandled export kind '{kind}'",
                  {"reason": "internal error"})


# ---------------------------------------------------------------------------
# ecad_to_3d  {doc_id, into_doc_id?, opts?} -> {step_path, part_doc_id?}
# ---------------------------------------------------------------------------

def _ecad_to_3d(params: dict, _emit: EmitFn) -> dict:
    from engine.ecad import session as esess, schema
    doc_id = str(_require(params, "doc_id"))
    into_doc_id = params.get("into_doc_id")
    opts: dict = params.get("opts") or {}

    s = esess.get_session(doc_id)
    if not schema.is_board(s.doc):
        raise OpError("invalid_op", "ecad_to_3d requires a pcb document",
                      {"reason": "wrong doc kind"})

    # Try engine.ecad.threed (a single .py module: board_to_step / board_to_part_doc are functions).
    try:
        import engine.ecad.threed as _threed_mod  # type: ignore
    except ImportError:
        # threed module not yet built — return a structured not-available error.
        raise OpError("not_available",
                      "ecad_to_3d (board→STEP) is not available in this build "
                      "(engine.ecad.threed not present)",
                      {"reason": "module not present", "feature": "engine.ecad.threed"})

    try:
        # board_to_step(board, out_path, opts=None) -> dict
        import tempfile, pathlib
        out_dir = opts.get("out_dir") or tempfile.mkdtemp(prefix="ecad_to_3d_")
        step_path = str(pathlib.Path(out_dir) / f"{s.doc.get('name', s.doc_id)}.step")
        step_result = _threed_mod.board_to_step(s.doc, step_path, opts or None)
        # board_to_step returns a dict; the actual path may be in 'path' or we use step_path directly
        actual_step = step_result.get("path", step_path) if isinstance(step_result, dict) else step_path
        result: dict = {"step_path": actual_step}

        # Optionally import the STEP into an existing CAD doc session.
        if into_doc_id:
            try:
                from engine.doc import session as docsess
                cad_s = docsess.get_session(into_doc_id)
                part_result = _threed_mod.board_to_part_doc(s.doc, actual_step)
                result["part_doc_id"] = part_result.get("doc_id") if isinstance(part_result, dict) \
                    else str(part_result)
            except Exception as exc:  # noqa: BLE001
                result["part_doc_id_warning"] = f"board_to_part_doc failed: {exc}"

        return result

    except OpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OpError("internal",
                      f"ecad_to_3d failed: {exc}",
                      {"reason": str(exc)}) from exc


# ---------------------------------------------------------------------------
# ecad_update_from_sch  {board_doc_id, sch_doc_id, prune?, place_origin?}
#   -> ops-response (for the board) + report
# ---------------------------------------------------------------------------

def _ecad_update_from_sch(params: dict, _emit: EmitFn) -> dict:
    """Back-annotation: populate a PCB from a linked schematic's netlist (ecad_ops.md §5).

    This is a CROSS-document operation (it needs both the board and the schematic), so it lives at
    the RPC/session layer rather than inside ops.py's single-doc apply. It:

      1. loads both open sessions (``board_doc_id`` must be a pcb, ``sch_doc_id`` a sch),
      2. builds the schematic's netlist (engine.ecad.netlist.build_netlist),
      3. reconciles via engine.ecad.sync.update_board_from_sch (add/update footprints, bind pad
         nets, register missing nets, report removed parts),
      4. swaps the new board into the board session (one revision bump, undo snapshot, recompile),
      5. returns the board ops-response with the sync ``report`` attached.

    Idempotent: a second call with no schematic changes adds/updates nothing.
    """
    from engine.ecad import session as esess, schema
    from engine.ecad import netlist as netlist_mod
    from engine.ecad import sync as sync_mod

    board_doc_id = str(_require(params, "board_doc_id"))
    sch_doc_id = str(_require(params, "sch_doc_id"))
    prune = bool(params.get("prune", False))
    place_origin = params.get("place_origin")
    base_revision = params.get("base_revision")

    board_s = esess.get_session(board_doc_id)
    sch_s = esess.get_session(sch_doc_id)

    if not schema.is_board(board_s.doc):
        raise OpError("invalid_op",
                      "ecad_update_from_sch: board_doc_id is not a pcb document",
                      {"reason": "wrong doc kind", "doc_id": board_doc_id})
    if not schema.is_schematic(sch_s.doc):
        raise OpError("invalid_op",
                      "ecad_update_from_sch: sch_doc_id is not a sch document",
                      {"reason": "wrong doc kind", "doc_id": sch_doc_id})

    netlist = netlist_mod.build_netlist(sch_s.doc)
    # Carry the schematic's id so sync records board.linked_sch = the source schematic.
    netlist = dict(netlist)
    netlist["id"] = sch_s.doc.get("id")

    result = sync_mod.update_board_from_sch(
        board_s.doc, netlist, place_origin=place_origin, prune=prune)
    report = result["report"]

    warnings: list[str] = []
    if report.get("unresolved"):
        warnings.append("unresolved footprints (no def found): "
                        + ", ".join(f"{ref}({fpid})" for ref, fpid in report["unresolved"]))
    if report.get("removed") and not prune:
        warnings.append("footprints no longer in schematic (not pruned): "
                        + ", ".join(report["removed"]))

    resp = esess.swap_doc(board_s, result["mutated_board"],
                          base_revision=base_revision, warnings=warnings or None)
    resp["report"] = report
    resp["doc_id"] = board_doc_id
    return resp


# ---------------------------------------------------------------------------
# Method + structured-error registration
# ---------------------------------------------------------------------------

METHODS: dict[str, object] = {
    "ecad_new": _ecad_new,
    "ecad_open": _ecad_open,
    "ecad_save": _ecad_save,
    "ecad_close": _ecad_close,
    "ecad_apply_ops": _ecad_apply_ops,
    "ecad_rebuild": _ecad_rebuild,
    "ecad_tree": _ecad_tree,
    "ecad_undo": _ecad_undo,
    "ecad_redo": _ecad_redo,
    "ecad_resolve_pick": _ecad_resolve_pick,
    "ecad_describe": _ecad_describe,
    "ecad_export": _ecad_export,
    "ecad_to_3d": _ecad_to_3d,
    "ecad_update_from_sch": _ecad_update_from_sch,
}

STRUCTURED_ERROR_METHODS: set[str] = set(METHODS.keys())
