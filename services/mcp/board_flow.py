"""Deterministic schematic -> preliminary-PCB flow (Wave 2b).

``board_from_schematic`` is the pure engine behind the MCP tool of the same name.
It reuses the already-landed E1 placer (:func:`engine.ecad.place.place_board`) and
E2 trace optimizer (:func:`engine.ecad.trace_opt.optimize_traces`) plus the existing
RPC helpers for schematic-to-board sync and autorouting. No placer/router is
reimplemented here.

Pipeline (seeded / deterministic)::

    1. resolve schematic session
    2. create/resolve a PCB session
    3. ``_ecad_update_from_sch``  — footprints + pad nets from the schematic netlist
    4. ``place_board`` (E1)      — connectivity-aware placement
    5. ``_ecad_autoroute``       — route the ratsnest
    6. ``optimize_traces`` (E2)  — arc corners / teardrops / impedance (optional)
    7. DRC                       — residual violations returned honestly (never hidden)

The MCP wrappers in :mod:`services.mcp.ecad_tools` call this module and wrap the
result in the standard compact ``ok/hint/next_actions`` envelope.
"""
from __future__ import annotations

from typing import Any, Optional, Union

# session/schema/drc/sync/autoroute are C++-dispatched — consumed here via the
# core_bridge-backed facades in ecad_common. place/trace_opt have NO C++ counterpart
# (they live under services/) — the direct imports STAY.
from services.ecad import place as place_mod  # STAYS: no C++ counterpart
from services.ecad import trace_opt as trace_opt_mod  # STAYS: no C++ counterpart

from services.mcp.ecad_common import (
    OpError,
    _compile_of,
    _rpc,
    _update_from_sch,
    ecad_session,
    schema,
)


# --------------------------------------------------------------------------- helpers


def _resolve_sch(sch_session_or_doc: Any) -> ecad_session.EcadSession:
    """Accept an EcadSession, a doc_id string, or a schematic doc dict."""
    if isinstance(sch_session_or_doc, ecad_session.EcadSession):
        s = sch_session_or_doc
    elif isinstance(sch_session_or_doc, str):
        s = ecad_session.get_session(sch_session_or_doc)
    elif isinstance(sch_session_or_doc, dict):
        # Live session already owning this dict, or a bare doc we need to wrap.
        for cand in ecad_session.iter_sessions():
            if cand.doc is sch_session_or_doc or cand.doc.get("id") == sch_session_or_doc.get("id"):
                s = cand
                break
        else:
            # Register a transient session so RPC can load it by doc_id.
            s = ecad_session.new_session("sch", sch_session_or_doc.get("name") or "sch")
            # Swap the provided doc in (validated).
            ecad_session.swap_doc(s, sch_session_or_doc)
    else:
        raise OpError(
            "invalid_op",
            "board_from_schematic: sch_session_or_doc must be an EcadSession, "
            "doc_id str, or schematic dict",
            {"reason": "bad sch argument", "got": type(sch_session_or_doc).__name__},
        )

    if not schema.is_schematic(s.doc):
        raise OpError(
            "invalid_op",
            f"board_from_schematic: expected a schematic, got kind={s.kind!r}",
            {"reason": "wrong doc kind", "doc_id": s.doc_id},
        )
    return s


def _resolve_pcb(opts: dict) -> ecad_session.EcadSession:
    """Create a fresh PCB or reuse ``opts['pcb_doc_id']`` / ``opts['pcb_session']``."""
    pcb_sess = opts.get("pcb_session")
    if isinstance(pcb_sess, ecad_session.EcadSession):
        if not schema.is_board(pcb_sess.doc):
            raise OpError("invalid_op", "pcb_session is not a board document",
                          {"reason": "wrong doc kind", "doc_id": pcb_sess.doc_id})
        return pcb_sess

    pcb_doc_id = opts.get("pcb_doc_id") or opts.get("board_doc_id") or ""
    if pcb_doc_id:
        s = ecad_session.get_session(str(pcb_doc_id))
        if not schema.is_board(s.doc):
            raise OpError("invalid_op",
                          f"pcb_doc_id {pcb_doc_id!r} is not a board document",
                          {"reason": "wrong doc kind", "doc_id": pcb_doc_id})
        return s

    name = str(opts.get("pcb_name") or opts.get("name") or "Board1")
    return ecad_session.new_session("pcb", name)


def _drc_residuals(session) -> list[dict]:
    """Compact residual DRC violations (errors first). Never suppress.

    Takes the board SESSION: the C++ session compile runs engine.ecad.drc on
    every apply/tree, so the residual read is one ecad_tree RPC."""
    try:
        viols = _compile_of(session).get("drc") or []
    except Exception as exc:  # noqa: BLE001 — DRC is advisory; surface failure honestly
        return [{"rule": "drc_unavailable", "severity": "error",
                 "message": f"{type(exc).__name__}: {exc}", "items": []}]
    if isinstance(viols, dict):
        viols = viols.get("violations", []) or []
    out: list[dict] = []
    for v in viols:
        if not isinstance(v, dict):
            continue
        out.append({
            "rule": v.get("rule", "?"),
            "severity": v.get("severity", "warning"),
            "message": v.get("message", ""),
            "items": list(v.get("items") or [])[:6],
        })
    # Errors first, then stable by rule/message for determinism.
    out.sort(key=lambda r: (0 if r.get("severity") == "error" else 1,
                            str(r.get("rule") or ""),
                            str(r.get("message") or "")))
    return out


def _placement_snapshot(board: dict) -> list[tuple]:
    """Stable (ref, id, x, y, angle) tuples for determinism checks."""
    rows = []
    for fp in board.get("footprints") or []:
        pos = fp.get("pos") or [0.0, 0.0]
        rows.append((
            str(fp.get("ref") or ""),
            str(fp.get("id") or ""),
            round(float(pos[0]), 6),
            round(float(pos[1]), 6),
            round(float(fp.get("angle") or 0.0), 6),
        ))
    rows.sort()
    return rows


def _routing_snapshot(board: dict) -> list[tuple]:
    """Stable track endpoint signature for determinism checks."""
    rows = []
    for t in board.get("tracks") or []:
        p1 = t.get("p1") or [0.0, 0.0]
        p2 = t.get("p2") or [0.0, 0.0]
        rows.append((
            str(t.get("net") or ""),
            str(t.get("layer") or ""),
            str(t.get("kind") or "segment"),
            round(float(p1[0]), 6), round(float(p1[1]), 6),
            round(float(p2[0]), 6), round(float(p2[1]), 6),
            round(float(t.get("width") or 0.0), 6),
        ))
    rows.sort()
    return rows


def _rect_outline_shapes(x0: float, y0: float, x1: float, y1: float) -> list[dict]:
    """Closed rectangular Edge.Cuts outline as four line shapes."""
    corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return [
        {
            "type": "line",
            "p1": corners[i],
            "p2": corners[(i + 1) % 4],
            "layer": "Edge.Cuts",
        }
        for i in range(4)
    ]


def _parts_bbox(doc: dict) -> tuple[float, float, float, float] | None:
    """Bounding box of placed footprints (origins + pad offsets), or None."""
    xs: list[float] = []
    ys: list[float] = []
    for fp in doc.get("footprints", []) or []:
        pos = fp.get("pos")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
            continue
        fx, fy = float(pos[0]), float(pos[1])
        added = False
        for pad in fp.get("pads", []) or []:
            off = pad.get("pos") or pad.get("at") or pad.get("offset")
            if isinstance(off, (list, tuple)) and len(off) >= 2:
                xs.append(fx + float(off[0]))
                ys.append(fy + float(off[1]))
                added = True
        if not added:
            xs.append(fx)
            ys.append(fy)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _ensure_outline(
    pcb,
    report: dict,
    warnings: list,
    *,
    margin: float = 5.0,
    min_side: float = 20.0,
    board_size: list | tuple | None = None,
    outline: list | None = None,
    area_scale: float = 1.0,
    force: bool = False,
) -> None:
    """Add a closed rectangular Edge.Cuts outline enclosing placed parts if absent.

    mega-27: tight AUTO-OUTLINE (margin=5) packs components so the router cannot
    meet 0.2mm clearance. Honor explicit ``outline``, ``board_size``, larger
    ``margin``, and ``area_scale`` (>=1) so force-chain can request >=3x component
    bbox area. ``force=True`` replaces an existing outline (outline-grow retry).
    """
    doc = pcb.doc
    if (doc.get("outline") or {}).get("shapes") and not force and outline is None:
        report["steps"].append({"step": "set_outline", "ok": True, "kept": True})
        return

    x0 = y0 = x1 = y1 = 0.0
    used = "auto"
    if isinstance(outline, list) and len(outline) >= 3:
        # Explicit polygon [[x,y],...] — convert to line chain.
        shapes = []
        for i in range(len(outline)):
            p1 = outline[i]
            p2 = outline[(i + 1) % len(outline)]
            if not (isinstance(p1, (list, tuple)) and isinstance(p2, (list, tuple))):
                continue
            shapes.append({
                "type": "line",
                "p1": [float(p1[0]), float(p1[1])],
                "p2": [float(p2[0]), float(p2[1])],
                "layer": "Edge.Cuts",
            })
        if len(shapes) >= 3:
            xs = [float(p[0]) for p in outline if isinstance(p, (list, tuple)) and len(p) >= 2]
            ys = [float(p[1]) for p in outline if isinstance(p, (list, tuple)) and len(p) >= 2]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            used = "explicit"
            try:
                ecad_session.apply_ops(
                    pcb, pcb.revision,
                    [{"op": "pcb_set_outline", "shapes": shapes, "closed": True}])
                outline_area = max(0.0, (x1 - x0) * (y1 - y0))
                report["steps"].append({
                    "step": "set_outline", "ok": True, "used": used,
                    "size_mm": [round(x1 - x0, 1), round(y1 - y0, 1)],
                    "outline_area_mm2": round(outline_area, 2),
                })
                report["outline_area_mm2"] = round(outline_area, 2)
                report["outline_size_mm"] = [round(x1 - x0, 1), round(y1 - y0, 1)]
            except Exception as e:  # noqa: BLE001
                report["steps"].append({
                    "step": "set_outline", "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                })
                warnings.append(f"set_outline failed: {type(e).__name__}: {e}")
            return

    if board_size is not None and len(board_size) >= 2:
        w = max(float(board_size[0]), float(min_side))
        h = max(float(board_size[1]), float(min_side))
        ox, oy = doc.get("origin") or [0.0, 0.0]
        x0, y0 = float(ox), float(oy)
        x1, y1 = x0 + w, y0 + h
        used = "board_size"
    else:
        ext = _parts_bbox(doc)
        m = max(0.0, float(margin))
        if ext is None:
            x0, y0, x1, y1 = 0.0, 0.0, float(min_side), float(min_side)
        else:
            x0, y0, x1, y1 = ext
            x0 -= m
            y0 -= m
            x1 += m
            y1 += m
            if x1 - x0 < min_side:
                cx = (x0 + x1) / 2.0
                x0, x1 = cx - min_side / 2.0, cx + min_side / 2.0
            if y1 - y0 < min_side:
                cy = (y0 + y1) / 2.0
                y0, y1 = cy - min_side / 2.0, cy + min_side / 2.0
        used = "parts+margin"

    # area_scale expands about center (mega-27 outline-grow / >=3x room).
    scale = max(1.0, float(area_scale or 1.0))
    if scale > 1.0 + 1e-9:
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        hw = (x1 - x0) * (scale ** 0.5) / 2.0
        hh = (y1 - y0) * (scale ** 0.5) / 2.0
        x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
        used = f"{used}*scale{scale:g}"

    shapes = _rect_outline_shapes(x0, y0, x1, y1)
    outline_area = max(0.0, (x1 - x0) * (y1 - y0))
    parts = _parts_bbox(doc)
    parts_area = 0.0
    if parts is not None:
        parts_area = max(0.0, (parts[2] - parts[0]) * (parts[3] - parts[1]))
    try:
        ecad_session.apply_ops(
            pcb, pcb.revision,
            [{"op": "pcb_set_outline", "shapes": shapes, "closed": True}])
        report["steps"].append({
            "step": "set_outline",
            "ok": True,
            "used": used,
            "size_mm": [round(x1 - x0, 1), round(y1 - y0, 1)],
            "outline_area_mm2": round(outline_area, 2),
            "component_bbox_area_mm2": round(parts_area, 2),
            "margin_mm": float(margin),
            "area_scale": float(scale),
        })
        report["outline_area_mm2"] = round(outline_area, 2)
        report["component_bbox_area_mm2"] = round(parts_area, 2)
        report["outline_size_mm"] = [round(x1 - x0, 1), round(y1 - y0, 1)]
    except Exception as e:  # noqa: BLE001
        report["steps"].append({"step": "set_outline", "ok": False,
                                "error": f"{type(e).__name__}: {e}"})
        warnings.append(f"set_outline failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- public


def board_from_schematic(
    sch_session_or_doc: Any,
    opts: Optional[dict] = None,
) -> dict:
    """Deterministic preliminary-PCB pipeline from a schematic.

    Parameters
    ----------
    sch_session_or_doc:
        Live :class:`~engine.ecad.session.EcadSession`, schematic ``doc_id`` string,
        or a schematic document dict.
    opts:
        Optional knobs::

            seed: int = 0
            optimize: bool = True          # run E2 optimize_traces after autoroute
            pcb_doc_id / board_doc_id: str # reuse an open board instead of creating one
            pcb_session: EcadSession       # same, as a live session
            pcb_name: str = "Board1"       # name for a freshly created board
            place_opts: dict               # forwarded to place_board
            route_opts: dict               # forwarded to _ecad_autoroute
            optimize_opts / targets: dict  # forwarded to optimize_traces
            prune: bool = False            # update_from_sch prune flag
            place_origin: [x, y]           # update_from_sch place_origin
            board_size: [w, h]             # mega-27: placeable area + outline size (mm)
            outline_margin: float = 5.0    # mega-27: Edge.Cuts margin beyond parts
            outline_area_scale: float = 1  # mega-27: expand outline area about center
            outline: [[x,y], ...]          # mega-27: explicit closed polygon outline
            force_outline: bool = False    # mega-27: replace existing outline (grow)

    Returns
    -------
    dict
        ``{board_doc_id, placed, routed, residual_drc, report, ok, ...}`` where
        ``report`` carries ``ratsnest_before/after``, ``corners_rounded``,
        ``teardrops``, ``nets_tuned``, ``drc_errors`` (plus placer/router detail).
        Residual DRC violations are listed explicitly — never hidden.
    """
    opts = dict(opts or {})
    seed = int(opts.get("seed", 0))
    do_optimize = bool(opts.get("optimize", True))

    sch = _resolve_sch(sch_session_or_doc)
    pcb = _resolve_pcb(opts)

    report: dict[str, Any] = {
        "seed": seed,
        "sch_doc_id": sch.doc_id,
        "board_doc_id": pcb.doc_id,
        "sync": {},
        "place": {},
        "route": {},
        "optimize": {},
        "ratsnest_before": 0,
        "ratsnest_after": 0,
        "corners_rounded": 0,
        "teardrops": 0,
        "nets_tuned": 0,
        "drc_errors": 0,
        "steps": [],
        "failed_nets": [],
        "unresolved": [],
    }
    warnings: list[str] = []

    # ---- 1. sync schematic -> board (footprints + pad nets) ----------------
    try:
        sync_resp = _update_from_sch(
            pcb, sch,
            prune=bool(opts.get("prune", False)),
            place_origin=(opts["place_origin"] if opts.get("place_origin") is not None
                          else None),
        )  # the helper swaps the board back and keeps pcb current
        sync_rep = dict(sync_resp.get("report") or {})
        report["sync"] = sync_rep
        report["unresolved"] = list(sync_rep.get("unresolved") or [])
        added = list(sync_rep.get("added") or [])
        updated = list(sync_rep.get("updated") or [])
        compile_info = sync_resp.get("compile") or {}
        rn_sync = compile_info.get("ratsnest") or []
        report["ratsnest_before"] = len(rn_sync) if isinstance(rn_sync, list) else int(
            sync_rep.get("ratsnest") or 0)
        report["steps"].append({
            "step": "update_from_sch",
            "ok": True,
            "added": len(added),
            "updated": len(updated),
            "unresolved": len(report["unresolved"]),
        })
        for w in sync_resp.get("warnings") or []:
            warnings.append(str(w))
    except OpError as e:
        report["steps"].append({"step": "update_from_sch", "ok": False,
                                "error": e.message})
        residual = _drc_residuals(pcb)
        return {
            "ok": False,
            "board_doc_id": pcb.doc_id,
            "sch_doc_id": sch.doc_id,
            "placed": 0,
            "routed": 0,
            "residual_drc": residual,
            "report": report,
            "error": e.message,
            "warnings": warnings,
            "placement": _placement_snapshot(pcb.doc),
            "routing": _routing_snapshot(pcb.doc),
        }

    # ---- 2. connectivity-aware placement (E1) ------------------------------
    place_opts = dict(opts.get("place_opts") or {})
    place_opts.setdefault("seed", seed)
    # mega-27: top-level board_size also drives the placer when no outline yet.
    if opts.get("board_size") is not None and "board_size" not in place_opts:
        place_opts["board_size"] = opts["board_size"]
    # Pre-seed outline BEFORE place when an explicit size/polygon is given so
    # the placer's placeable bbox is the generous room (not the default grid).
    _pre_outline = opts.get("outline")
    _pre_board_size = opts.get("board_size") or place_opts.get("board_size")
    if _pre_outline is not None or _pre_board_size is not None:
        try:
            _ensure_outline(
                pcb, report, warnings,
                margin=float(opts.get("outline_margin", 5.0) or 5.0),
                board_size=_pre_board_size,
                outline=_pre_outline if isinstance(_pre_outline, list) else None,
                area_scale=float(opts.get("outline_area_scale", 1.0) or 1.0),
                force=bool(opts.get("force_outline", False)),
            )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"pre_outline failed: {type(e).__name__}: {e}")
    try:
        place_result = place_mod.place_board(pcb.doc, place_opts)
        placed_doc = place_result["doc"]
        place_rep = dict(place_result.get("report") or {})
        report["place"] = place_rep
        # Write back via swap_doc so session revision/undo stay consistent.
        ecad_session.swap_doc(
            pcb, placed_doc, base_revision=pcb.revision,
            warnings=[
                f"place_board: moved {place_rep.get('moved', 0)}/"
                f"{place_rep.get('n_footprints', 0)}; "
                f"ratsnest={place_rep.get('ratsnest_len_mm', 0):.2f} mm"
            ],
        )
        report["steps"].append({
            "step": "place_board",
            "ok": True,
            "moved": place_rep.get("moved", 0),
            "n_footprints": place_rep.get("n_footprints", 0),
            "ratsnest_len_mm": place_rep.get("ratsnest_len_mm", 0.0),
            "overlaps": place_rep.get("overlaps", 0),
        })
        # Prefer post-place ratsnest length for "before routing" metric if available.
        if place_rep.get("ratsnest_len_mm") is not None and report["ratsnest_before"] == 0:
            # Keep airwire COUNT from sync; length is supplemental.
            report["place"]["ratsnest_len_mm"] = place_rep.get("ratsnest_len_mm")
    except OpError as e:
        report["steps"].append({"step": "place_board", "ok": False, "error": e.message})
        warnings.append(f"place_board failed: {e.message}")
    except Exception as e:  # noqa: BLE001
        report["steps"].append({
            "step": "place_board", "ok": False,
            "error": f"{type(e).__name__}: {e}",
        })
        warnings.append(f"place_board failed: {type(e).__name__}: {e}")

    n_placed = len(pcb.doc.get("footprints") or [])

    # ---- 2b. ensure a closed Edge.Cuts outline (ecad_to_3d / fill need it) --
    # If pre-outline already set a generous board, keep it unless force_outline.
    _ensure_outline(
        pcb, report, warnings,
        margin=float(opts.get("outline_margin", 5.0) or 5.0),
        board_size=opts.get("board_size") or place_opts.get("board_size"),
        outline=opts.get("outline") if isinstance(opts.get("outline"), list) else None,
        area_scale=float(opts.get("outline_area_scale", 1.0) or 1.0),
        force=bool(opts.get("force_outline", False)),
    )

    # Count airwires after placement (ratsnest_before for the route step).
    try:
        rn_pre = _compile_of(pcb).get("ratsnest") or []  # C++ compile runs drc.ratsnest
        report["ratsnest_before"] = len(rn_pre)
    except Exception:  # noqa: BLE001
        pass

    # ---- 3. autoroute ------------------------------------------------------
    route_opts = dict(opts.get("route_opts") or {})
    # Keep routing deterministic: fixed seed if the router accepts one.
    route_opts.setdefault("seed", seed)
    routed_nets: list = []
    failed_nets: list = []
    try:
        ar_resp = _rpc("ecad_autoroute",
                       {"doc_id": pcb.doc_id, "opts": route_opts})
        pcb._update(ar_resp)  # keep pcb.doc/revision current after the RPC mutation
        ar_rep = dict(ar_resp.get("report") or {})
        report["route"] = ar_rep
        routed_nets = list(ar_rep.get("routed") or [])
        failed_nets = list(ar_rep.get("failed") or [])
        report["failed_nets"] = failed_nets
        report["ratsnest_after"] = int(
            ar_rep.get("airwires_after", ar_rep.get("ratsnest_after", 0)) or 0)
        if "airwires_before" in ar_rep:
            report["ratsnest_before"] = int(ar_rep.get("airwires_before") or 0)
        report["steps"].append({
            "step": "autoroute",
            "ok": True,
            "routed": len(routed_nets),
            "failed": len(failed_nets),
            "airwires_before": report["ratsnest_before"],
            "airwires_after": report["ratsnest_after"],
        })
        for w in ar_resp.get("warnings") or []:
            warnings.append(str(w))
    except OpError as e:
        report["steps"].append({"step": "autoroute", "ok": False, "error": e.message})
        warnings.append(f"autoroute failed: {e.message}")
    except Exception as e:  # noqa: BLE001
        report["steps"].append({
            "step": "autoroute", "ok": False,
            "error": f"{type(e).__name__}: {e}",
        })
        warnings.append(f"autoroute failed: {type(e).__name__}: {e}")

    # ---- 4. trace optimization (E2) ----------------------------------------
    opt_rep: dict = {
        "corners_rounded": 0,
        "teardrops": 0,
        "nets_tuned": 0,
        "drc_ok": True,
        "reverted": 0,
        "skipped": True,
    }
    if do_optimize:
        opt_opts = dict(opts.get("optimize_opts") or {})
        if opts.get("targets") is not None:
            opt_opts["targets"] = opts["targets"]
        try:
            opt_result = trace_opt_mod.optimize_traces(pcb.doc, opt_opts)
            opt_doc = opt_result["doc"]
            opt_rep = dict(opt_result.get("report") or {})
            opt_rep["skipped"] = False
            report["optimize"] = opt_rep
            report["corners_rounded"] = int(opt_rep.get("corners_rounded") or 0)
            report["teardrops"] = int(opt_rep.get("teardrops") or 0)
            report["nets_tuned"] = int(opt_rep.get("nets_tuned") or 0)
            ecad_session.swap_doc(
                pcb, opt_doc, base_revision=pcb.revision,
                warnings=[
                    f"optimize_traces: corners={report['corners_rounded']}, "
                    f"teardrops={report['teardrops']}, "
                    f"nets_tuned={report['nets_tuned']}"
                ],
            )
            report["steps"].append({
                "step": "optimize_traces",
                "ok": True,
                "corners_rounded": report["corners_rounded"],
                "teardrops": report["teardrops"],
                "nets_tuned": report["nets_tuned"],
            })
        except Exception as e:  # noqa: BLE001
            report["steps"].append({
                "step": "optimize_traces", "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })
            warnings.append(f"optimize_traces failed: {type(e).__name__}: {e}")
            report["optimize"] = {"error": f"{type(e).__name__}: {e}", "skipped": False}
    else:
        report["optimize"] = opt_rep
        report["steps"].append({
            "step": "optimize_traces", "ok": True, "skipped": True,
        })

    # ---- 5. residual DRC (honest) ------------------------------------------
    residual = _drc_residuals(pcb)
    n_err = sum(1 for v in residual if v.get("severity") == "error")
    report["drc_errors"] = n_err
    report["drc_warnings"] = sum(1 for v in residual if v.get("severity") != "error")
    report["steps"].append({
        "step": "drc",
        "ok": n_err == 0,
        "errors": n_err,
        "warnings": report["drc_warnings"],
    })

    # Final ratsnest after full pipeline (may still have unrouted airwires).
    try:
        rn_final = _compile_of(pcb).get("ratsnest") or []
        report["ratsnest_after"] = len(rn_final)
    except Exception:  # noqa: BLE001
        pass

    n_tracks = len(pcb.doc.get("tracks") or [])
    steps_ok = all(s.get("ok") for s in report["steps"]
                   if s.get("step") != "drc")  # residual DRC does not fail the flow

    return {
        "ok": steps_ok,
        "board_doc_id": pcb.doc_id,
        "sch_doc_id": sch.doc_id,
        "revision": pcb.revision,
        "placed": n_placed,
        "routed": len(routed_nets) if routed_nets else n_tracks,
        "routed_nets": routed_nets,
        "failed_nets": failed_nets,
        "residual_drc": residual,
        "report": report,
        "warnings": warnings,
        # Determinism helpers (tests compare these).
        "placement": _placement_snapshot(pcb.doc),
        "routing": _routing_snapshot(pcb.doc),
        "n_tracks": n_tracks,
        "n_footprints": n_placed,
        # mega-27 C27: outline room evidence for AC4 grade axes
        "outline_area_mm2": report.get("outline_area_mm2"),
        "component_bbox_area_mm2": report.get("component_bbox_area_mm2"),
        "outline_size_mm": report.get("outline_size_mm"),
    }


def autoplace_board(pcb_session_or_doc: Any, opts: Optional[dict] = None) -> dict:
    """Connectivity-aware placement on an active board (standalone tool helper).

    Accepts a PCB session (:class:`services.mcp.ecad_common.EcadSession`) or ``doc_id``.
    Pure :func:`engine.ecad.place.place_board` + ``swap_doc`` write-back (the
    write-back goes through the C++-backed session facade).
    """
    opts = dict(opts or {})
    opts.setdefault("seed", int(opts.get("seed", 0)))

    if isinstance(pcb_session_or_doc, ecad_session.EcadSession):
        session = pcb_session_or_doc
    elif isinstance(pcb_session_or_doc, str):
        session = ecad_session.get_session(pcb_session_or_doc)
    else:
        raise OpError("invalid_op",
                      "autoplace_board requires a pcb session or doc_id",
                      {"reason": "bad pcb argument"})

    if not schema.is_board(session.doc):
        raise OpError("invalid_op", "autoplace_board requires a pcb document",
                      {"reason": "wrong doc kind", "doc_id": session.doc_id})

    # engine.ecad.place.autoplace inlined: it wrote back through the deleted
    # Python session registry; here place_board (pure) + the C++-backed facade swap_doc.
    _pl = place_mod.place_board(session.doc, opts)
    place_rep = dict(_pl.get("report") or {})
    resp = ecad_session.swap_doc(
        session, _pl["doc"], base_revision=session.revision,
        warnings=[
            f"autoplace: moved {place_rep.get('moved', 0)}/{place_rep.get('n_footprints', 0)} "
            f"footprints; ratsnest={place_rep.get('ratsnest_len_mm', 0):.2f} mm; "
            f"overlaps={place_rep.get('overlaps', 0)}"
        ],
    )
    residual = _drc_residuals(session)
    return {
        "ok": True,
        "board_doc_id": session.doc_id,
        "revision": session.revision,
        "placed": place_rep.get("n_footprints", len(session.doc.get("footprints") or [])),
        "moved": place_rep.get("moved", 0),
        "report": place_rep,
        "residual_drc": residual,
        "placement": _placement_snapshot(session.doc),
        "warnings": list(resp.get("warnings") or []),
    }


def optimize_board_traces(
    pcb_session_or_doc: Any,
    opts: Optional[dict] = None,
) -> dict:
    """Arc/teardrop/impedance pass on an active board (standalone tool helper).

    Accepts a PCB session or ``doc_id``. Pure E2 :func:`optimize_traces` +
    ``swap_doc`` write-back.
    """
    opts = dict(opts or {})

    if isinstance(pcb_session_or_doc, ecad_session.EcadSession):
        session = pcb_session_or_doc
    elif isinstance(pcb_session_or_doc, str):
        session = ecad_session.get_session(pcb_session_or_doc)
    else:
        raise OpError("invalid_op",
                      "optimize_board_traces requires a pcb session or doc_id",
                      {"reason": "bad pcb argument"})

    if not schema.is_board(session.doc):
        raise OpError("invalid_op", "optimize_board_traces requires a pcb document",
                      {"reason": "wrong doc kind", "doc_id": session.doc_id})

    result = trace_opt_mod.optimize_traces(session.doc, opts)
    new_doc = result["doc"]
    opt_rep = dict(result.get("report") or {})
    swap = ecad_session.swap_doc(
        session, new_doc, base_revision=session.revision,
        warnings=[
            f"optimize_traces: corners={opt_rep.get('corners_rounded', 0)}, "
            f"teardrops={opt_rep.get('teardrops', 0)}, "
            f"nets_tuned={opt_rep.get('nets_tuned', 0)}"
        ],
    )
    residual = _drc_residuals(session)
    n_err = sum(1 for v in residual if v.get("severity") == "error")
    return {
        "ok": True,
        "board_doc_id": session.doc_id,
        "revision": session.revision if swap is None else session.revision,
        "report": opt_rep,
        "corners_rounded": int(opt_rep.get("corners_rounded") or 0),
        "teardrops": int(opt_rep.get("teardrops") or 0),
        "nets_tuned": int(opt_rep.get("nets_tuned") or 0),
        "residual_drc": residual,
        "drc_errors": n_err,
        "routing": _routing_snapshot(session.doc),
        "warnings": list(swap.get("warnings") or []) if isinstance(swap, dict) else [],
    }
