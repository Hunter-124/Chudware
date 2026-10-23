"""ECAD PCB MCP tools (split out of ecad_tools.py): ecad_new_pcb, run_drc, run_hostcheck, run_dfm, autoroute, zones, export, to_3d, net classes, stackup, make_board, board_from_schematic, autoplace, optimize_traces."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import json
import math
import os

from .cad_common import coerce_json_or_repr, require_list, require_mapping
from .ecad_common import (
    OpError,
    _ECAD,
    _apply,
    _attach_board_ascii_readback,
    _autosave_all_open_ecad,
    _budget_timeout_err,
    _compile_of,
    _default_out_dir,
    _default_outline_shapes,
    _dumps,
    _erc_lines,
    _erc_summary,
    _err,
    _get_pcb,
    _get_sch,
    _outline_bbox,
    _pad_world,
    _parts_extents,
    _rpc,
    _run_with_budget,
    _update_from_sch,
    _zone_polygon,
    ecad_session,
    netlist_mod,
)

# Gerber filename extensions per layer id (Protel/KiCad fab-house standard) — the C++
# ecad_export RPC returns {layer_id: content}; the file naming mirrors
# engine.ecad.fab.gerber._LAYER_EXT so exported filenames are unchanged.
_GERBER_LAYER_EXT: dict[str, str] = {
    "F.Cu": ".GTL", "B.Cu": ".GBL",
    "F.SilkS": ".GTO", "B.SilkS": ".GBO",
    "F.Mask": ".GTS", "B.Mask": ".GBS",
    "F.Paste": ".GTP", "B.Paste": ".GBP",
    "Edge.Cuts": ".GM1",
    "F.Fab": ".GM2", "B.Fab": ".GM3",
    "F.CrtYd": ".GM4", "B.CrtYd": ".GM5",
}


def _gerber_ext(layer_id: str) -> str:
    if layer_id in _GERBER_LAYER_EXT:
        return _GERBER_LAYER_EXT[layer_id]
    import re
    m = re.match(r"^In(\d+)\.Cu$", layer_id)
    if m:
        return f".G{int(m.group(1)) + 1}"
    return f".G_{layer_id.replace('.', '_')}"


def _resolve_out_path(path: str) -> str:
    """Canonicalize a caller-supplied output path/dir: expanduser + abspath, mirroring
    cad_exchange_tools.py's _sanitize_output_path so a relative path resolves against THIS
    process's cwd (not some other daemon's) and the caller always learns the real location."""
    return os.path.abspath(os.path.expanduser(path))


def _file_sizes(paths: dict) -> tuple[dict, int]:
    """{key: path} -> ({key: bytes-on-disk (0 if missing)}, total bytes) — confirms every
    claimed write actually landed rather than trusting the writer's return value."""
    sizes = {k: (os.path.getsize(p) if os.path.exists(p) else 0) for k, p in paths.items()}
    return sizes, sum(sizes.values())


def _write_fab_files(session, kind: str, out_dir: str) -> dict[str, str]:
    """C++ ecad_export (gerber/drill) -> files on disk; returns {layer_id|pth/npth -> path},
    mirroring engine.ecad.fab.write_gerbers / write_drill."""
    resp = _rpc("ecad_export", {"kind": kind, "doc_id": session.doc_id})
    contents = resp.get("files") or {}
    board_name = (session.doc.get("name", "board") or "board").replace(" ", "_")
    os.makedirs(out_dir, exist_ok=True)
    out: dict[str, str] = {}
    for key, content in contents.items():
        fn = (f"{board_name}{_gerber_ext(key)}" if kind == "gerber"
              else f"{board_name}-{key.upper()}.drl")
        p = os.path.join(out_dir, fn)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        out[key] = p
    return out


def _fab_text(session, kind: str, file_key: str) -> str:
    """C++ ecad_export single-text kinds (bom/cpl) -> the text payload."""
    resp = _rpc("ecad_export", {"kind": kind, "doc_id": session.doc_id})
    return (resp.get("files") or {}).get(file_key, "")


def _board_bbox(doc: dict) -> tuple[float, float, float, float]:
    """Copper extent + a small pad — local port of engine.ecad.autoroute._board_bbox's
    no-outline fallback (there is no per-helper RPC; used to size a whole-board pour)."""
    xs: list[float] = []
    ys: list[float] = []
    ext = _parts_extents(doc)  # footprint origins + pad offsets
    if ext is not None:
        xs += [ext[0], ext[2]]
        ys += [ext[1], ext[3]]
    for t in doc.get("tracks", []) or []:
        for key in ("p1", "p2"):
            p = t.get(key)
            if isinstance(p, (list, tuple)) and len(p) == 2:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    for v in doc.get("vias", []) or []:
        p = v.get("at")
        if isinstance(p, (list, tuple)) and len(p) == 2:
            xs.append(float(p[0]))
            ys.append(float(p[1]))
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    pad = 2.0
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


# --------------------------------------------------------------- manual-routing helpers
#
# Pad resolution for ecad_route_pad_to_pad. Kept module-level (not inside the register
# closure) so the routing tool stays readable and the lookups are unit-testable.


class _PadRefError(Exception):
    """A pad reference that cannot be resolved. Carries the payload the tool returns."""

    def __init__(self, message: str, *, hint: str = "",
                 next_actions: Optional[list] = None, **extra) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.next_actions = next_actions or []
        self.extra = extra


def _fp_by_ref(doc: dict, ref: str) -> Optional[dict]:
    """Placed footprint by ref-des ('R1') or internal id ('fp1'). Refs are how models think."""
    token = str(ref).strip()
    if not token:
        return None
    for fp in doc.get("footprints", []) or []:
        if str(fp.get("ref") or "") == token:
            return fp
    for fp in doc.get("footprints", []) or []:
        if str(fp.get("id") or "") == token:
            return fp
    low = token.lower()
    for fp in doc.get("footprints", []) or []:
        if str(fp.get("ref") or "").lower() == low:
            return fp
    return None


def _pad_by_number(fp: dict, number) -> Optional[dict]:
    """Pad of a placed footprint by its pad NUMBER ('1', 'A2', 2) or internal id ('p3')."""
    token = str(number).strip()
    if not token:
        return None
    pads = fp.get("pads", []) or []
    for pad in pads:
        if str(pad.get("number") or "") == token:
            return pad
    for pad in pads:
        if str(pad.get("id") or "") == token:
            return pad
    low = token.lower()
    for pad in pads:
        if str(pad.get("number") or "").lower() == low:
            return pad
    return None


def _resolve_pad(doc: dict, ref: str, number, role: str) -> tuple[dict, dict, tuple]:
    """(footprint, pad, world_xy) for ``ref``.``number``, or raise _PadRefError naming the
    concrete alternatives. NEVER falls back to a nearby pad — a wrong pad is a wrong net."""
    fp = _fp_by_ref(doc, ref)
    if fp is None:
        refs = [str(f.get("ref") or f.get("id")) for f in doc.get("footprints", []) or []]
        raise _PadRefError(
            f"{role}: no footprint {str(ref)!r} on the board",
            hint=("Board footprints are: " + (", ".join(sorted(refs)) or "(none)") +
                  ". Refs come from the schematic via ecad_update_from_sch."),
            next_actions=["ecad_update_from_sch()  # push schematic parts onto the board",
                          "ecad_status()  # list what is actually placed"],
            board_refs=sorted(refs))
    pad = _pad_by_number(fp, number)
    if pad is None:
        nums = [str(p.get("number") or p.get("id")) for p in fp.get("pads", []) or []]
        raise _PadRefError(
            f"{role}: footprint {fp.get('ref') or fp.get('id')} has no pad {str(number)!r}",
            hint=(f"{fp.get('ref') or fp.get('id')} ({fp.get('fpid')}) pads are: "
                  + (", ".join(nums) or "(none)") + "."),
            next_actions=["ecad_status()  # inspect the placed footprints and their pads"],
            pads=nums)
    return fp, pad, _pad_world(fp, pad)


def _copper_layers(doc: dict) -> list[str]:
    n = doc.get("stackup", {}).get("copper_layers", 2)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 2
    if n <= 1:
        return ["F.Cu"]
    return ["F.Cu", *[f"In{i}.Cu" for i in range(1, n - 1)], "B.Cu"]


def _net_class_of(doc: dict, net: str) -> str:
    for n in doc.get("nets", []) or []:
        if n.get("name") == net:
            return str(n.get("class") or "default")
    return "default"


def _class_track_width(doc: dict, net: str):
    """The net-class ``track_width`` ecad_stackup_solve writes, or None when unsolved."""
    classes = doc.get("net_classes") or {}
    cls = classes.get(_net_class_of(doc, net))
    if not isinstance(cls, dict):
        cls = classes.get("default") if isinstance(classes.get("default"), dict) else {}
    w = cls.get("track_width")
    try:
        return float(w) if w is not None else None
    except (TypeError, ValueError):
        return None


def register_ecad_pcb_tools(mcp: FastMCP) -> None:
    """ECAD PCB MCP tools (split out of ecad_tools.py): ecad_new_pcb, run_drc, run_hostcheck, run_dfm, autoroute, zones, export, to_3d, net classes, stackup, make_board, board_from_schematic, autoplace, optimize_traces."""


    @mcp.tool()
    def ecad_new_pcb(name: str = "Board1") -> str:
        """Start a NEW PCB (board) document and make it the active one.

        Returns the doc id + stackup summary. A fresh board has a 2-layer copper stackup, a
        default net class, and a GND net but no footprints or outline yet. Use ecad_find_part to
        discover footprint fpids, place them, route, then ecad_run_drc.
        """
        s = ecad_session.new_session("pcb", name)
        _ECAD["current_pcb"] = s.doc_id
        _ECAD["current"] = s.doc_id
        n_cu = s.doc.get("stackup", {}).get("copper_layers", 2)
        return _dumps({
            "ok": True, "doc_id": s.doc_id, "kind": "pcb", "name": name,
            "revision": s.revision, "copper_layers": n_cu, "footprints": 0,
            "nets": [n.get("name") for n in s.doc.get("nets", [])],
            "warnings": [],
            "hint": f"Empty {n_cu}-layer board. Place footprints (ecad_find_part finds fpids), "
                    "route tracks, then ecad_run_drc to check spacing.",
            "next_actions": [
                "ecad_find_part('0603')  # discover footprint fpids",
                "ecad_run_drc()  # at any time, see clearance/width violations",
            ],
        })



    # ---------------------------------------------------------------- DRC

    @mcp.tool()
    def ecad_run_drc(doc_id: str = "") -> str:
        """Run the Design Rule Check on the active board and return a summary with the worst
        offenders (clearance, track width, via size, annular ring, edge clearance, etc.).

        DRC is advisory. The grouped ``by_rule`` view + the readable lines tell you which physical
        spacing/size rule each track/pad/via breaks so you can widen, move, or re-space it.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a board first.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        res = _compile_of(session).get("drc", [])  # C++ compile runs engine.ecad.drc
        violations = res if isinstance(res, list) else res.get("violations", [])
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)
        worst = lines[:5]
        verdict = (f"{summary['errors']} error(s), {summary['warnings']} warning(s)."
                   + (" worst: " + " | ".join(worst) if worst else " Clean board."))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warnings": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "warnings": [],
            "hint": verdict,
            "next_actions": (
                ["Address the worst DRC offenders above (widen tracks / increase spacing / "
                 "move parts), then re-run ecad_run_drc()."] if violations else
                ["ecad_export('gerber')  # board passes DRC — generate fab outputs"]
            ),
        })



    # ---------------------------------------------------------------- host-board rule pack

    @mcp.tool()
    def ecad_run_hostcheck(doc_id: str = "") -> str:
        """Run the MCU/FPGA host-board rule pack on the active board — the
        device-level checks that brick a host board even when DRC/ERC are clean.

        Rule families (stable ids, all findings carry an actionable fix hint):
          host_decoupling_missing/_far/_via  per-power-pin decoupling coverage (value band,
                                             distance budget, same-side/via-quality heuristic)
          host_bulk_cap                      bulk capacitance (>= 4.7 uF) per supply rail
          host_sequencing[/_unparsed/_unverifiable]  power-sequencing requirements from the
                                             component's eng metadata
                                             (contracts/ecad/boardphys.md — 'VCORE before VIO')
                                             validated against the board's actual enable/PGOOD
                                             chains; NO hardcoded part knowledge
          host_xtal_loadcap/_loadcap_match/_loop/_keepout  crystal load caps present + matched,
                                             loop-area and other-net-track keepout heuristics
          host_reset_pull/_cap               reset line pull resistor + filter cap
          host_bootstrap_float/_shared       boot-strap pins strapped to a valid level and not
                                             shared with an actively-driven signal

        Advisory, like DRC. Hosts are found via eng metadata (eng.supply.rails) or
        power_in pins; seed metadata via the library or per-instance sim_params.eng
        overrides. Thresholds can be tuned per board via board rules 'hostcheck'
        (decoupling_dist_mm, decoupling_min_f/max_f, bulk_min_f, xtal_loop_dist_mm,
        xtal_keepout_mm). Also runs inside ecad_make_board(hostcheck=True).
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a board first.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        try:
            from services.ecad import hostboard_rules as hb_mod  # STAYS: no C++ counterpart
            violations = hb_mod.run_hostcheck(doc)
        except Exception as e:  # noqa: BLE001
            return _err(f"hostcheck engine error: {e}", hint="Inspect with ecad_status().")
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)
        hints = [v["hint"] for v in violations if v.get("severity") == "error"][:5] or \
                [v["hint"] for v in violations][:5]
        verdict = (f"{summary['errors']} error(s), {summary['warnings']} warning(s)."
                   + (" worst: " + " | ".join(lines[:5]) if lines else
                      " Host-board rules clean."))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warning_count": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "fix_hints": hints,
            "warnings": [],
            "hint": verdict + (" " + hints[0] if hints else ""),
            "next_actions": (
                ["Apply the fix_hints above (add/move decoupling, strap pins, wire the "
                 "enable chain), then re-run ecad_run_hostcheck()."] if violations else
                ["ecad_run_drc()  # host rules clean — check the geometric rules too"]
            ),
        })



    # ---------------------------------------------------------------- DFM

    @mcp.tool()
    def ecad_run_dfm(doc_id: str = "") -> str:
        """Run the Design-For-Manufacturing (DFM) check suite on the active board and return a
        readable summary grouped by rule (counts + worst offenders) with a severity split.

        DFM is advisory: violations are warnings or (for courtyard_overlap) errors but never block
        editing. Rules checked: silk_over_pad, silk_min_width, silk_min_text, mask_sliver,
        acid_trap, courtyard_overlap, annular_min (DFM grade), drill_min (DFM grade).

        The reading tells you which rule to fix first: courtyard_overlap (component collision) is
        always fix-first, followed by drill_min errors, then warnings.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="DFM needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        # DFM walks every pad/track/courtyard — on a dense board it can run long. Budget-guard so an
        # overrun never times out the transport and kills the server (Gap (b)).
        _autosave_all_open_ecad()
        status, dfm_resp = _run_with_budget(
            lambda: _rpc("ecad_dfm", {"doc_id": session.doc_id}))
        if status == "timeout":
            return _budget_timeout_err(
                "DFM check",
                simplify="The board is large/dense — run DFM on a simpler board or after thinning "
                         "silk/copper, or check DRC first (ecad_run_drc).",
                next_actions=["ecad_run_drc()", "ecad_status()"])
        if status == "error":
            return _err(f"DFM engine error: {dfm_resp}", hint="Inspect with ecad_status().")
        violations = dfm_resp.get("violations", []) if isinstance(dfm_resp, dict) else []
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)

        # group by rule: count + worst message
        worst_errors = [v for v in violations if v.get("severity") == "error"][:3]
        worst_lines = lines[:5]

        # Build a priority-ordered fix list.
        fix_order: list[str] = []
        # courtyard_overlap first (components collide -> assembly fails)
        cy_group = next((g for g in summary["groups"] if g["rule"] == "courtyard_overlap"), None)
        if cy_group:
            items = cy_group.get("items", [])[:2]
            fix_order.append(
                f"fix courtyard_overlap first -- components collide "
                f"({cy_group['count']}x, items: {', '.join(map(str, items))})")
        for g in summary["groups"]:
            if g["rule"] == "courtyard_overlap":
                continue
            if g["severity"] == "error":
                fix_order.append(
                    f"fix {g['rule']} ({g['count']}x): "
                    f"{g['messages'][0] if g['messages'] else ''}")
        for g in summary["groups"]:
            if g["severity"] != "error":
                fix_order.append(
                    f"review {g['rule']} ({g['count']} warning(s))")

        verdict = (f"{summary['errors']} DFM error(s), {summary['warnings']} warning(s)."
                   + (" worst: " + " | ".join(worst_lines) if worst_lines else " Clean."))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warning_count": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "fix_priority": fix_order,
            "warnings": worst_lines[:3],
            "reading": verdict,
            "hint": verdict + (" " + fix_order[0] if fix_order else ""),
            "next_actions": (
                fix_order[:2] if fix_order else
                ["ecad_export('gerber')  # DFM clean -- proceed to fab"]
            ),
        })



    # ---------------------------------------------------------------- autoroute

    @mcp.tool()
    def ecad_autoroute(pitch: float = 0.0,
                       diff_pairs: Optional[Union[list, str]] = None,
                       ripup_passes: int = 2,
                       min_clearance: float = 0.0,
                       doc_id: str = "") -> str:
        """Auto-route the active board's ratsnest with a basic 2-layer grid (Lee/A*) router.

        Connects the unrouted airwires with orthogonal tracks + through-vias, respecting clearance.
        Honest about limits: failed nets are reported by name and need manual routing or more space.

        ``pitch`` (mm) overrides the grid resolution (default 0.25); finer threads tighter gaps
        but is slower. Run ecad_update_from_sch first so there is a ratsnest to route.

        ``diff_pairs`` controls differential-pair routing:
          - omit (default): auto-detect pairs by *_P/*_N, *+/*- naming
          - [['CLK_P','CLK_N'], ...]: route specific named pairs coupled
          - 'auto': same as omitting (explicit auto-detect)

        ``ripup_passes`` (default 2): how many rip-up-and-reroute sweeps to attempt after the
        greedy pass; 0 for the original single-pass behaviour.

        ``min_clearance`` (mm, default 0): floor on the clearance halo. When >0 (e.g. 0.2), the
        router never places track centres closer than clearance+half-width using at least this
        floor — matching the DRC rule even if a net class is looser. mega-28: close AC4.

        Returns per-pair skew/coupling stats in ``diff_pair_stats`` when pairs are routed.

        AUTO-READBACK: response includes compact layer ASCII + net_delta (tracks/vias/crossings).
        READ it before the next board edit. Call render_board_ascii before declaring done.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Routing needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        opts: dict = {}
        if pitch and pitch > 0:
            opts["pitch"] = float(pitch)
        opts["ripup_passes"] = max(0, int(ripup_passes))
        if min_clearance and float(min_clearance) > 0:
            opts["min_clearance"] = float(min_clearance)
        # diff_pairs: None/'auto'/'' -> let the router auto-detect; a list -> pass through
        # M3: diff_pairs may be the string 'auto' (documented alias for auto-detect);
        # treat any non-list value as "auto" so callers that pass 'auto' don't get a
        # Pydantic/iteration error trying to iterate over a string.
        if diff_pairs is not None and not isinstance(diff_pairs, str):
            opts["diff_pairs"] = [[str(p), str(n)] for p, n in (
                (entry[0], entry[1]) for entry in diff_pairs if len(entry) >= 2)]
        # The Lee/A* router over a fine grid is one of the heaviest ECAD ops — budget-guard so an
        # overrun never times out the transport and kills the server (Gap (b)).
        _autosave_all_open_ecad()
        status, resp = _run_with_budget(lambda: _rpc(
            "ecad_autoroute", {"doc_id": session.doc_id, "opts": opts}))
        if status == "timeout":
            return _budget_timeout_err(
                "autoroute",
                simplify="Give the router less to do: route fewer nets, use a coarser pitch "
                         "(ecad_autoroute(pitch=0.3)), or place parts with more room.",
                next_actions=["ecad_autoroute(pitch=0.3)  # coarser grid is much faster",
                              "ecad_status()"])
        if status == "error":
            e = resp  # on 'error', _run_with_budget returns the raised exception
            if isinstance(e, OpError):
                return _err(f"autoroute failed: {e.message}", hint=e.message,
                            next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
            raise e
        report = resp.get("report", {})
        routed = report.get("routed", [])
        failed = report.get("failed", [])
        before = report.get("airwires_before", 0)
        after = report.get("airwires_after", 0)
        warnings = list(resp.get("warnings", []))
        ripup_iters = report.get("ripup_iterations", 0)
        dp_stats = report.get("diff_pairs", [])

        # Build a human-readable summary of diff-pair results.
        dp_reading_lines: list[str] = []
        for dp in dp_stats:
            # The autoroute engine uses 'nets' list and 'routed' bool.
            nets = dp.get("nets", [])
            net_p = nets[0] if len(nets) > 0 else dp.get("net_p", "?")
            net_n = nets[1] if len(nets) > 1 else dp.get("net_n", "?")
            skew = dp.get("skew_mm")
            coupled_pct = dp.get("coupled_pct")
            dp_routed = dp.get("routed", dp.get("status") == "ok")
            status_str = "ok" if dp_routed else "failed"
            line = f"Pair {net_p}/{net_n}: status={status_str}"
            if skew is not None:
                line += f", skew={skew:.3f}mm"
            if coupled_pct is not None:
                line += f", coupled={coupled_pct:.0f}%"
            dp_reading_lines.append(line)

        head = (f"routed {len(routed)} net(s), {len(failed)} failed; ratsnest "
                f"{before} -> {after} airwire(s); {report.get('vias', 0)} via(s), "
                f"{report.get('length_mm', 0.0)} mm of track"
                + (f"; rip-up rescued {ripup_iters} attempt(s)" if ripup_iters else "")
                + (f"; {len(dp_stats)} diff pair(s)" if dp_stats else "") + ".")
        next_actions: list[str] = []
        if failed:
            next_actions.append(
                f"{len(failed)} net(s) failed ({', '.join(map(str, failed[:6]))}) — "
                "route them by hand pad-by-pad, or give the router more space "
                "(move parts apart / finer pitch).")
            next_actions.append(
                "ecad_route_pad_to_pad('U1', '1', 'R1', '2')  # hand-route one failed "
                "connection; the net is detected from the pads")
            next_actions.append("ecad_autoroute(pitch=0.15)  # retry with a finer grid")
        else:
            next_actions.append("ecad_fill_zones()  # pour copper, then ecad_run_drc()")
        if dp_stats and any(not dp.get("routed", True) for dp in dp_stats):
            next_actions.append(
                "Check diff-pair results: skew > 0.2mm or coupled_pct < 80 means "
                "the pair needs manual tuning or more board space.")
        payload = {
            "ok": True, "doc_id": session.doc_id, "revision": resp.get("revision"),
            "routed_nets": routed, "failed_nets": failed,
            "airwires_before": before, "airwires_after": after,
            "ratsnest_remaining": after,
            "vias": report.get("vias", 0), "length_mm": report.get("length_mm", 0.0),
            "grid": report.get("grid", {}),
            "ripup_iterations": ripup_iters,
            "diff_pair_stats": dp_stats,
            "diff_pair_reading": "; ".join(dp_reading_lines) if dp_reading_lines else None,
            "warnings": warnings,
            "hint": head + (" All nets routed." if not failed else
                            f" {len(failed)} net(s) failed — manual routing or more space needed.")
                  + (" " + "; ".join(dp_reading_lines) if dp_reading_lines else "")
                  + " READ the ascii_readback before the next board edit.",
            "next_actions": next_actions,
        }
        _attach_board_ascii_readback(payload, doc_id=session.doc_id)
        return _dumps(payload)


    # ------------------------------------------- surgical differential-pair routing

    @mcp.tool()
    def ecad_route_diff_pair(net_p: str, net_n: str,
                             opts: Optional[Union[dict, str]] = None,
                             doc_id: str = "") -> str:
        """Route ONE differential pair without disturbing the rest of an already-routed board.

        Use this instead of ``ecad_autoroute`` when adding or re-routing one pair on an
        already-routed board, without ripping up other nets. ``net_p`` and ``net_n`` are the
        exact board net names (for example ``USB_D+`` and ``USB_D-``).

        Confirmed ``opts`` keys from the C++ router are: ``diff_gap`` (edge-to-edge pair gap,
        mm), ``length_tol`` (maximum desired length skew, mm), ``pitch`` (routing-grid pitch,
        mm), ``max_grid_cells``, ``via_cost``, ``bend_cost``, and ``min_clearance`` or
        ``min_clearance_mm`` (clearance floor, mm). Track widths come from each net's class;
        this entry point has no width or layer option. ``opts`` may be a mapping or a JSON
        object string.

        The response reports ``routed``, ``length_p``, ``length_n``, ``skew_mm``, and
        ``coupled_pct``. Do not infer length matching merely from a successful RPC: inspect
        ``routed`` and compare ``skew_mm`` with the requested ``length_tol``; ``coupled_pct``
        states how much of the result remained parallel-coupled.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Differential-pair routing needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])

        pair = (str(net_p or "").strip(), str(net_n or "").strip())
        if not pair[0] or not pair[1]:
            return _err("net_p and net_n are both required",
                        hint="Pass the exact two board net names, e.g. "
                             "ecad_route_diff_pair('USB_D+', 'USB_D-').",
                        next_actions=["ecad_status()  # inspect board nets"],
                        board_changed=False)
        if pair[0] == pair[1]:
            return _err("net_p and net_n must name two different nets",
                        hint="A differential pair needs distinct positive and negative nets.",
                        next_actions=["ecad_status()  # inspect board nets"],
                        board_changed=False)

        raw_opts = opts
        if isinstance(raw_opts, str):
            try:
                raw_opts = coerce_json_or_repr(raw_opts)
            except (TypeError, ValueError, SyntaxError) as e:
                return _err(f"opts must be a JSON object: {e}",
                            hint='Use opts=\'{"diff_gap": 0.2, "length_tol": 0.1}\'.',
                            board_changed=False)
        if raw_opts is None:
            route_opts = {}
        elif isinstance(raw_opts, dict):
            route_opts = raw_opts
        else:
            return _err(f"opts must be an object, got {type(raw_opts).__name__}",
                        hint='Use opts={"diff_gap": 0.2, "length_tol": 0.1}.',
                        board_changed=False)

        try:
            resp = _rpc("ecad_route_diff_pair",
                        {"doc_id": session.doc_id, "net_p": pair[0], "net_n": pair[1],
                         "opts": route_opts or {}})
        except OpError as e:
            return _err(f"differential-pair route failed: {e.message}",
                        hint="Check that both names exactly match board nets with unrouted "
                             "airwires, then retry with more room or a finer pitch.",
                        next_actions=["ecad_status()  # inspect board nets and ratsnest",
                                      f"ecad_route_diff_pair({pair[0]!r}, {pair[1]!r}, "
                                      "opts={'pitch': 0.15})"],
                        detail=getattr(e, "detail", {}), board_changed=False)

        report = resp.get("report") or {}
        routed = bool(report.get("routed", False))
        skew = report.get("skew_mm")
        coupled = report.get("coupled_pct")
        reading = f"Pair {pair[0]}/{pair[1]}: status={'ok' if routed else 'failed'}"
        if skew is not None:
            reading += f", skew={float(skew):.3f}mm"
        if coupled is not None:
            reading += f", coupled={float(coupled):.0f}%"
        if not routed:
            reading += (". No pair was routed: verify both exact net names have unrouted "
                        "airwires and that the board has enough free space.")

        payload = {
            "ok": routed,
            "doc_id": session.doc_id,
            "revision": resp.get("revision"),
            "net_p": pair[0],
            "net_n": pair[1],
            "routed": routed,
            "length_p": report.get("length_p", 0.0),
            "length_n": report.get("length_n", 0.0),
            "skew_mm": skew,
            "coupled_pct": coupled,
            "warnings": list(resp.get("warnings", [])),
            "error": (None if routed else
                      f"could not route differential pair {pair[0]}/{pair[1]}"),
            "hint": reading,
            "next_actions": (["ecad_run_drc()  # verify pair clearance",
                              "render_board_ascii()  # inspect the routed pair"]
                             if routed else
                             ["ecad_status()  # verify exact net names and unrouted airwires",
                              f"ecad_route_diff_pair({pair[0]!r}, {pair[1]!r}, "
                              "opts={'pitch': 0.15})  # retry on a finer grid"]),
        }
        _attach_board_ascii_readback(payload, doc_id=session.doc_id)
        return _dumps(payload)


    # ------------------------------------------------- manual (pad-to-pad) routing

    @mcp.tool()
    def ecad_route_pad_to_pad(from_ref: str = "", from_pad: Union[str, int] = "",
                              to_ref: str = "", to_pad: Union[str, int] = "",
                              layer: str = "F.Cu", to_layer: str = "",
                              width: float = 0.0,
                              via_at: Optional[Union[list, str]] = None,
                              doc_id: str = "") -> str:
        """Route ONE copper trace between two pads by hand — the surgical complement to
        ecad_autoroute, and the tool autoroute's "failed net" report is telling you to use.

        ``from_ref``/``to_ref`` are ref-des ('U1', 'R3') or internal footprint ids; ``from_pad``/
        ``to_pad`` are pad NUMBERS ('1', 'A2') as printed in the footprint, or pad ids. Same
        endpoint spelling as the schematic-side ecad_wire(from_ref, from_pin, ...).

        THE NET IS DETECTED, NEVER ASSERTED. Both pads' nets are read off the board and must be
        the SAME net; routing two different nets together would short them, so that is REFUSED,
        not warned about. A pad with no net is refused too (run ecad_update_from_sch first).

        WIDTH PRECEDENCE: explicit ``width`` (mm) > the net class's solved ``track_width``
        (what ecad_stackup_solve writes, which autoroute and DRC already honour) > the board
        default 0.25 mm. Omit ``width`` to inherit the solved impedance-controlled width — the
        response reports the width actually used and where it came from (``width_source``).

        LAYER CHANGE: pass ``to_layer`` to finish on a different copper layer. A through via is
        inserted at ``via_at`` ([x, y]) or, by default, the midpoint of the two pads, and the
        trace becomes pad -> via on ``layer`` plus via -> pad on ``to_layer``.

        The segments go into the board's own ``tracks`` list (the same pcb_add_track ops
        autoroute emits), so ecad_run_drc sees them: a hand trace that violates clearance WILL
        be reported. Response carries per-segment layer/width/endpoints and the total length_mm.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Manual routing needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc

        missing = [n for n, v in (("from_ref", from_ref), ("from_pad", from_pad),
                                  ("to_ref", to_ref), ("to_pad", to_pad))
                   if not str(v).strip()]
        if missing:
            return _err(f"missing required endpoint argument(s): {', '.join(missing)}",
                        hint="Name both pads: from_ref/from_pad and to_ref/to_pad, e.g. "
                             "ecad_route_pad_to_pad('U1', '3', 'R5', '1').",
                        next_actions=["ecad_status()  # list placed refs and their pads"])

        try:
            fp_a, pad_a, xy_a = _resolve_pad(doc, from_ref, from_pad, "from")
            fp_b, pad_b, xy_b = _resolve_pad(doc, to_ref, to_pad, "to")
        except _PadRefError as e:
            return _err(e.message, hint=e.hint, next_actions=e.next_actions, **e.extra)

        if pad_a is pad_b:
            return _err("from and to name the SAME pad — a track to itself is not a connection",
                        hint="Pick two distinct pads on the same net.",
                        next_actions=["ecad_status()"])

        net_a, net_b = pad_a.get("net"), pad_b.get("net")
        a_name = f"{fp_a.get('ref') or fp_a.get('id')}.{pad_a.get('number')}"
        b_name = f"{fp_b.get('ref') or fp_b.get('id')}.{pad_b.get('number')}"
        no_net = [n for n, v in ((a_name, net_a), (b_name, net_b)) if not v]
        if no_net:
            return _err(f"pad(s) with no net: {', '.join(no_net)}",
                        hint="A pad with no net has nothing to connect. Board pad nets come "
                             "from the schematic — wire the pins with ecad_wire, then push the "
                             "netlist onto the board with ecad_update_from_sch.",
                        next_actions=[f"ecad_wire('{fp_a.get('ref')}', '{pad_a.get('number')}', "
                                      f"'{fp_b.get('ref')}', '{pad_b.get('number')}')",
                                      "ecad_update_from_sch()"],
                        from_net=net_a, to_net=net_b)
        if net_a != net_b:
            return _err(f"REFUSED: {a_name} is on net {net_a!r} but {b_name} is on net "
                        f"{net_b!r} — routing them together would SHORT two nets",
                        hint="A copper trace between two different nets is a short circuit, so "
                             "this tool never does it. If they really should be one net, join "
                             "them on the SCHEMATIC first and re-sync; the board follows the "
                             "netlist, not the other way round.",
                        next_actions=[f"ecad_wire('{fp_a.get('ref')}', '{pad_a.get('number')}', "
                                      f"'{fp_b.get('ref')}', '{pad_b.get('number')}')  "
                                      "# join them on the schematic if that is the intent",
                                      "ecad_update_from_sch()  # then re-push the netlist",
                                      "ecad_autoroute()  # route each net to its own members"],
                        from_net=net_a, to_net=net_b, board_changed=False)
        net = str(net_a)

        cu = _copper_layers(doc)
        start_layer = str(layer or "F.Cu")
        end_layer = str(to_layer or "").strip() or start_layer
        bad = [l for l in {start_layer, end_layer} if l not in cu]
        if bad:
            return _err(f"layer(s) not in this board's stackup: {', '.join(sorted(bad))}",
                        hint=f"Copper layers on this {len(cu)}-layer board are: "
                             f"{', '.join(cu)}.",
                        next_actions=[f"ecad_route_pad_to_pad('{from_ref}', '{from_pad}', "
                                      f"'{to_ref}', '{to_pad}', layer='{cu[0]}')"],
                        copper_layers=cu, board_changed=False)

        # An SMD pad only has copper on the side it sits on: starting a trace on the other
        # layer would draw a track that touches nothing. Refuse rather than "connect" thin air.
        for pad, name, lyr, which in ((pad_a, a_name, start_layer, "from"),
                                      (pad_b, b_name, end_layer, "to")):
            pad_layers = [str(x) for x in (pad.get("layers") or [])]
            cu_of_pad = [x for x in pad_layers if x in cu]
            if pad.get("type") == "smd" and cu_of_pad and lyr not in cu_of_pad:
                return _err(f"{which} pad {name} has copper only on {', '.join(cu_of_pad)}, "
                            f"not on {lyr} — a trace there would connect to nothing",
                            hint=f"Route {name} on {cu_of_pad[0]}, or move the footprint to the "
                                 "other side of the board.",
                            next_actions=[f"ecad_route_pad_to_pad('{from_ref}', '{from_pad}', "
                                          f"'{to_ref}', '{to_pad}', layer='{cu_of_pad[0]}')"],
                            board_changed=False)

        # Width precedence: explicit > net-class solved track_width > board default.
        try:
            explicit_w = float(width or 0.0)
        except (TypeError, ValueError):
            return _err(f"width must be a number in mm, got {width!r}",
                        hint="Omit width to inherit the net class's solved track_width.")
        solved_w = _class_track_width(doc, net)
        if explicit_w > 0.0:
            width_source = "explicit"
        elif solved_w is not None:
            width_source = f"net_class:{_net_class_of(doc, net)}"
        else:
            width_source = "board_default"

        via_pt = None
        if start_layer != end_layer:
            raw_via = via_at
            if isinstance(raw_via, str) and raw_via.strip():
                raw_via = coerce_json_or_repr(raw_via)
            if raw_via is None:
                via_pt = [(xy_a[0] + xy_b[0]) / 2.0, (xy_a[1] + xy_b[1]) / 2.0]
            elif isinstance(raw_via, (list, tuple)) and len(raw_via) == 2:
                try:
                    via_pt = [float(raw_via[0]), float(raw_via[1])]
                except (TypeError, ValueError):
                    via_pt = None
            if via_pt is None:
                return _err(f"via_at must be [x, y] in mm, got {via_at!r}",
                            hint="Omit via_at to drop the layer-change via at the midpoint of "
                                 "the two pads.",
                            board_changed=False)

        def _track_op(p1, p2, lyr):
            op = {"op": "pcb_add_track", "net": net, "layer": lyr, "kind": "segment",
                  "p1": [float(p1[0]), float(p1[1])], "p2": [float(p2[0]), float(p2[1])]}
            # Omitting 'width' is what makes the C++ op resolve the net class's track_width.
            if explicit_w > 0.0:
                op["width"] = explicit_w
            return op

        if via_pt is None:
            ops = [_track_op(xy_a, xy_b, start_layer)]
        else:
            ops = [_track_op(xy_a, via_pt, start_layer),
                   {"op": "pcb_add_via", "net": net, "at": via_pt,
                    "layers": [start_layer, end_layer], "type": "through"},
                   _track_op(via_pt, xy_b, end_layer)]

        before_tracks = {t.get("id") for t in doc.get("tracks", []) or []}
        before_vias = {v.get("id") for v in doc.get("vias", []) or []}
        try:
            _apply(session, ops)
        except OpError as e:
            return _err(f"manual route failed: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}),
                        board_changed=False)

        doc = session.doc
        new_tracks = [t for t in doc.get("tracks", []) or []
                      if t.get("id") not in before_tracks]
        new_vias = [v for v in doc.get("vias", []) or []
                    if v.get("id") not in before_vias]
        segments = []
        total_len = 0.0
        for t in new_tracks:
            p1, p2 = t.get("p1") or [0, 0], t.get("p2") or [0, 0]
            seg_len = math.hypot(float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1]))
            total_len += seg_len
            segments.append({"id": t.get("id"), "layer": t.get("layer"),
                             "width_mm": round(float(t.get("width") or 0.0), 6),
                             "p1": [round(float(p1[0]), 6), round(float(p1[1]), 6)],
                             "p2": [round(float(p2[0]), 6), round(float(p2[1]), 6)],
                             "length_mm": round(seg_len, 6)})
        used_w = segments[0]["width_mm"] if segments else None
        total_len = round(total_len, 6)

        rats = (_compile_of(session).get("ratsnest") or {})
        remaining = rats.get("airwires") if isinstance(rats, dict) else None
        if isinstance(remaining, list):
            remaining = len(remaining)

        head = (f"routed {a_name} -> {b_name} on net {net}: {len(segments)} segment(s), "
                f"{total_len} mm at {used_w} mm wide ({width_source})"
                + (f", 1 via at [{via_pt[0]:.3f}, {via_pt[1]:.3f}] "
                   f"({start_layer}->{end_layer})" if new_vias else
                   f" on {start_layer}") + ".")
        payload = {
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net, "net_class": _net_class_of(doc, net),
            "from": {"ref": fp_a.get("ref") or fp_a.get("id"), "pad": pad_a.get("number"),
                     "at": [round(xy_a[0], 6), round(xy_a[1], 6)]},
            "to": {"ref": fp_b.get("ref") or fp_b.get("id"), "pad": pad_b.get("number"),
                   "at": [round(xy_b[0], 6), round(xy_b[1], 6)]},
            "segments": segments,
            "length_mm": total_len,
            "width_mm": used_w,
            "width_source": width_source,
            "solved_class_width_mm": solved_w,
            "vias": [{"id": v.get("id"), "at": v.get("at"), "layers": v.get("layers"),
                      "drill": v.get("drill"), "diameter": v.get("diameter")}
                     for v in new_vias],
            "ratsnest_remaining": remaining,
            "warnings": [],
            "hint": head + " This trace is a normal board track — ecad_run_drc() checks its "
                           "clearance and width like any routed copper.",
            "next_actions": [
                "ecad_run_drc()  # confirm the hand route keeps clearance",
                "ecad_autoroute()  # let the router finish whatever is still an airwire",
            ],
        }
        _attach_board_ascii_readback(payload, doc_id=session.doc_id)
        return _dumps(payload)



    # ---------------------------------------------------------------- zones

    @mcp.tool()
    def ecad_add_zone(net: str, layer: str = "F.Cu",
                      rect_or_polygon: Optional[Union[list, str]] = None, doc_id: str = "") -> str:
        """Add a copper pour (zone) bound to ``net`` on ``layer`` — usually a ground/power plane.

        ``rect_or_polygon`` is either a rectangle ``[x0, y0, x1, y1]`` (auto-expanded to 4 corners)
        or an explicit polygon ``[[x, y], ...]`` (>=3 points). Omit it to flood the whole board
        outline bounding box (a full-board GND pour, the common case). The pour is not filled until
        ecad_fill_zones runs (compile fills a coarse preview automatically after this edit).

        AUTO-READBACK: compact layer ASCII + findings after the zone is added. READ it before
        fill/DRC. Call render_board_ascii before declaring the board done.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Zones live on a board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        # SILENT-DISCARD guard: _zone_polygon only recognises a real list, so a
        # stringified "[x0,y0,x1,y1]"/python-repr polygon used to fall straight
        # through to the generic "could not derive a zone polygon" error instead
        # of being read as the shape it plainly is.
        if isinstance(rect_or_polygon, str) and rect_or_polygon.strip():
            rect_or_polygon = coerce_json_or_repr(rect_or_polygon)
            if isinstance(rect_or_polygon, str):
                return _err(f"rect_or_polygon is not valid JSON/list: {rect_or_polygon!r}",
                            hint="Pass [x0,y0,x1,y1] or [[x,y],...] (>=3 pts).")
        polygon = _zone_polygon(doc, rect_or_polygon)
        if polygon is None:
            return _err("could not derive a zone polygon",
                        hint="Pass rect_or_polygon as [x0,y0,x1,y1] or [[x,y],...] (>=3 pts), "
                             "or set a board outline first so the whole-board default works.",
                        next_actions=["ecad_status()"])
        # The zone op requires the net to already exist on the board (there is no add-net op). A
        # fresh board has GND; other nets arrive via ecad_update_from_sch. Guide the model rather
        # than emitting an op that would fail.
        if not any(n.get("name") == net for n in doc.get("nets", [])):
            existing = [n.get("name") for n in doc.get("nets", [])]
            return _err(f"net {net!r} is not on the board yet",
                        hint=f"Board nets are: {', '.join(existing) or '(none)'}. A fresh board "
                             "has GND; populate other nets with ecad_update_from_sch first, or "
                             "pour GND.",
                        next_actions=["ecad_add_zone('GND')  # the default ground net",
                                      "ecad_update_from_sch()  # bind schematic nets to the board"],
                        board_nets=existing)
        try:
            resp = _apply(session, [{"op": "pcb_add_zone", "net": net, "layer": layer,
                                     "polygon": polygon}])
        except OpError as e:
            return _err(f"could not add zone on {net!r}/{layer}: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
        zones = session.doc.get("zones", [])
        payload = {
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net, "layer": layer, "polygon": polygon,
            "zone_count": len(zones),
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Added a {net} copper pour on {layer} ({len(polygon)} vertices). "
                     "Fill it (ecad_fill_zones) to compute the actual copper, then ecad_run_drc. "
                     "READ the ascii_readback before the next board edit."),
            "next_actions": ["ecad_fill_zones()  # compute the poured copper",
                             "ecad_run_drc()  # confirm clearance to other-net copper"],
        }
        _attach_board_ascii_readback(payload, doc_id=session.doc_id, layer=layer)
        return _dumps(payload)



    @mcp.tool()
    def ecad_fill_zones(doc_id: str = "") -> str:
        """Fill every copper pour on the active board at FULL resolution and report the result.

        Computes each zone's copper polygons (outline clipped to the board, minus clearance around
        other-net copper, with thermal relief on same-net through-hole pads) and persists them — this
        is the copper that gets exported to Gerber. Reports zones filled + total copper area. The
        interactive rebuild already fills a coarse preview after each edit; call this before export
        for the precise fill.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Pours live on a board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        if not session.doc.get("zones"):
            return _err("the board has no copper pours to fill",
                        hint="Add a ground/power pour first.",
                        next_actions=["ecad_add_zone('GND')  # whole-board ground pour"])
        # Full-resolution zone fill rasterizes the whole board at 0.1mm (~1/res^2 cells) — a big or
        # fine-grid pour can run long. Budget-guard so an overrun never times out the transport and
        # kills the server (Gap (b); this was the make_board trigger in adb46f62).
        _autosave_all_open_ecad()
        status, resp = _run_with_budget(
            lambda: _rpc("ecad_fill_zones", {"doc_id": session.doc_id}))
        if status == "timeout":
            return _budget_timeout_err(
                "zone fill",
                simplify="The pour is large/fine — shrink the zone polygon or the board outline. "
                         "The interactive compile already keeps a coarse preview fill.",
                next_actions=["ecad_run_drc()", "ecad_status()"])
        if status == "error":
            e = resp
            if isinstance(e, OpError):
                return _err(f"zone fill failed: {e.message}", hint=e.message,
                            next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
            raise e
        summary = resp.get("fill_summary", {})
        filled = summary.get("zones_filled", 0)
        total = summary.get("zones", 0)
        area = summary.get("copper_area_mm2", 0.0)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": resp.get("revision"),
            "zones": total, "zones_filled": filled,
            "polygons": summary.get("polygons", 0),
            "copper_area_mm2": area,
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Filled {filled}/{total} pour(s): {area} mm^2 of copper across "
                     f"{summary.get('polygons', 0)} polygon(s). This is the copper Gerber exports."),
            "next_actions": ["ecad_run_drc()  # check the pour respects clearance",
                             "ecad_export('gerber')  # the fill is baked into the copper layers"],
        })



    # ---------------------------------------------------------------- export

    @mcp.tool()
    def ecad_export(kind: str, path: str = "", doc_id: str = "") -> str:
        """Export a fabrication output from the active board.

        ``kind`` is one of: 'gerber' (copper/mask/silk layer set), 'drill' (Excellon PTH/NPTH),
        'bom' (CSV bill of materials), 'placement' (pick-and-place CSV), or 'constraints'
        (FPGA pin-constraint files from the design netlist: native XDC for Xilinx / LPF for
        Lattice + a neutral CSV pinout per FPGA — the board is the source of truth for the
        HDL flow; see ecad_fpga_check). ``path`` is the output directory (gerber/drill/
        constraints) or file (bom/placement); defaults under the Chudware cache. Returns the
        written path(s).
        """
        kind = (kind or "").lower()
        if kind == "constraints":
            # constraints come from the SCHEMATIC netlist (pin->net truth), not the board
            try:
                session = _get_sch(doc_id or None)
            except OpError as e:
                return _err(e.message,
                            hint="Constraint export reads the schematic netlist (the placed "
                                 "FPGA symbol's pin->net map).",
                            next_actions=["ecad_new_schematic('myfpga')",
                                          "ecad_place_symbol('FPGA:iCE40UP5K-SG48', 100, 100)"])
            try:
                from services.ecad import fpga  # STAYS: no C++ counterpart
            except Exception as e:  # noqa: BLE001
                return _err(f"fpga module unavailable ({type(e).__name__}: {e})")
            nl = netlist_mod.build_netlist(session.doc)
            exports = fpga.export_constraints(nl)
            if not exports:
                return _err("no FPGA found on the schematic",
                            hint="Place an FPGA symbol (e.g. 'FPGA:iCE40UP5K-SG48') or tag a "
                                 "symbol with fields={'fpga_device': '<device>'}; "
                                 "known devices: " + ", ".join(
                                     d["device"] for d in fpga.load_devices().values()),
                            next_actions=["ecad_fpga_check()"])
            out_dir = _resolve_out_path(path) if path else str(_default_out_dir(session.doc.get("name", "board")))
            os.makedirs(out_dir, exist_ok=True)
            files = []
            for ref, texts in sorted(exports.items()):
                for ext, text in sorted(texts.items()):
                    fp = os.path.join(out_dir, f"{ref}_pins.{ext}")
                    with open(fp, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    files.append(fp)
            _ECAD["out_dir"] = out_dir
            file_sizes, total_bytes = _file_sizes({f: f for f in files})
            all_exist = all(file_sizes[f] > 0 for f in files) if files else False
            return _dumps({
                "ok": True, "kind": "constraints", "doc_id": session.doc_id,
                "dir": out_dir, "files": files, "file_bytes": file_sizes,
                "total_bytes": total_bytes, "exists": all_exist, "warnings": [],
                "hint": ("Wrote pin constraints for "
                         f"{', '.join(sorted(exports))}: {len(files)} file(s), {total_bytes} "
                         f"byte(s) total, confirmed on disk at {out_dir}. Native dialect "
                         "(XDC/LPF) + neutral CSV pinout. Feed the XDC/LPF straight to "
                         "Vivado/nextpnr."),
                "next_actions": ["ecad_fpga_check()  # bank/diff/clock/config validation"],
            })
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Export needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        out_dir = _resolve_out_path(path) if path else str(_default_out_dir(doc.get("name", "board")))
        try:
            if kind == "gerber":
                files = _write_fab_files(session, "gerber", out_dir)
                file_sizes, total_bytes = _file_sizes(files)
                result = {"kind": "gerber", "dir": out_dir, "files": files,
                          "file_bytes": file_sizes, "total_bytes": total_bytes,
                          "layer_count": len(files)}
            elif kind == "drill":
                files = _write_fab_files(session, "drill", out_dir)
                file_sizes, total_bytes = _file_sizes(files)
                result = {"kind": "drill", "dir": out_dir, "files": files,
                          "file_bytes": file_sizes, "total_bytes": total_bytes,
                          "layer_count": len(files)}
            elif kind == "bom":
                text = _fab_text(session, "bom", "bom")
                fp = _resolve_out_path(path) if path else os.path.join(out_dir, "bom.csv")
                os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                # M5: text.count("\n") includes the header line; subtract 1 to report
                # only DATA rows (the meaningful count for a 5-component board = 5, not 6).
                result = {"kind": "bom", "path": fp,
                          "rows": max(0, text.count("\n") - 1),
                          "bytes": os.path.getsize(fp) if os.path.exists(fp) else 0}
            elif kind == "placement":
                text = _fab_text(session, "cpl", "cpl")
                fp = _resolve_out_path(path) if path else os.path.join(out_dir, "placement.csv")
                os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                # M5: same header-line subtraction for placement CSV
                result = {"kind": "placement", "path": fp,
                          "rows": max(0, text.count("\n") - 1),
                          "bytes": os.path.getsize(fp) if os.path.exists(fp) else 0}
            else:
                return _err(f"unknown export kind {kind!r}",
                            hint="Use 'gerber', 'drill', 'bom', 'placement', or "
                                 "'constraints' (FPGA XDC/LPF/CSV).",
                            next_actions=["ecad_export('gerber')"])
        except Exception as e:  # noqa: BLE001
            return _err(f"export {kind} failed: {type(e).__name__}: {e}",
                        hint="Check ecad_run_drc() and that the board has footprints/copper.")
        _ECAD["out_dir"] = out_dir
        written_path = result.get("path") or result.get("dir")
        exists = (os.path.exists(result["path"]) if "path" in result
                  else bool(result.get("files")) and all(
                      os.path.exists(p) for p in result["files"].values()))
        size_note = (f"{result['bytes']} byte(s)" if "bytes" in result else
                     f"{result.get('layer_count', 0)} file(s), {result.get('total_bytes', 0)} byte(s) total")
        result.update({"ok": exists, "exists": exists, "doc_id": session.doc_id, "warnings": [],
                       "hint": f"Wrote {kind} to {written_path} ({size_note}), confirmed on disk.",
                       "next_actions": ["ecad_export('drill')  # the other fab outputs",
                                        "ecad_export('bom')"]})
        return _dumps(result)



    @mcp.tool()
    def ecad_to_3d(path: str = "", doc_id: str = "") -> str:
        """Export a 3D model (STEP) of the active board + the aicad.boardphys/1 sidecar.

        Calls ``engine.ecad.threed.board_to_step``: assembles the board substrate and a
        component BODY for every on-board footprint (real model_3d STEP > library package
        box/cylinder from the eng metadata > pad-extent proxy box) and writes a STEP file.
        Also writes ``<step>.boardphys.json`` — the machine-readable ``aicad.boardphys/1``
        sidecar mapping each component solid to its power/thermal metadata (power_w typ/max,
        theta_ja/jc, Tj/Ta limits, supply rails) with board power totals. Missing metadata is
        honest ``null``, never 0 (contracts/ecad/boardphys.md). Downstream consumers (thermal
        handoff, PDN, power budget, host-board rules) read the sidecar.

        Returns the STEP path, sidecar path, volume summary + power totals, and a hint telling
        the user to open the STEP as an import feature in the Chudware app so they can design an
        enclosure around the board.

        Falls back to a clean, actionable error (never raises) when the board has no outline or
        when there is no active PCB session.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="3D export needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc

        # Build the output path (default: Chudware cache dir named after the board).
        out_path = (_resolve_out_path(path) if path
                    else str(_default_out_dir(doc.get("name", "board")) / "board.step"))

        # The FIRST ecad_to_3d call cold-imports OCP/build123d (OpenCASCADE Python bindings) — that
        # import alone can take 10-30 s in a freshly-warmed server, which on its own overruns the
        # client transport timeout and gets the server KILLED before any STEP is written. Every
        # OTHER heavy ECAD tool (simulate/make_board/autoroute/fill/dfm) is budget-guarded; this one
        # was not. Run the import + geometry build on the bounded worker and autosave first, so an
        # overrun returns a clean "still running" message instead of a -32001 server kill.
        _autosave_all_open_ecad()

        def _build():
            from services.ecad.threed import board_to_step  # STAYS: no C++ counterpart; lazy import (OCP/build123d)
            return board_to_step(doc, out_path)

        bstatus, bval = _run_with_budget(_build)
        if bstatus == "timeout":
            return _budget_timeout_err(
                "3D STEP export",
                simplify="The 3D exporter is still loading/building (the first call cold-loads the "
                         "OCP/build123d CAD kernel). Retry ecad_to_3d() — the kernel is warm now — "
                         "or use ecad_export('gerber') for fab output.",
                next_actions=["ecad_to_3d()  # retry; the CAD kernel is now loaded",
                              "ecad_export('gerber')"],
                doc_id=session.doc_id)
        if bstatus == "error":
            e = bval
            if isinstance(e, ImportError):
                return _err(f"3D exporter unavailable (ImportError: {e})",
                            hint="engine.ecad.threed could not be imported — "
                                 "OCP/build123d may not be installed in this environment. "
                                 "Use ecad_export('gerber') for fab output.",
                            next_actions=["ecad_export('gerber')", "ecad_export('drill')"],
                            available=False)
            return _err(f"3D export failed: {type(e).__name__}: {e}",
                        hint="Check that the board has a closed Edge.Cuts outline "
                             "(pcb_set_outline op, closed=True). "
                             "An empty outline or open chain raises BoardGeometryError.",
                        next_actions=["ecad_run_drc()  # confirms outline is present",
                                      "ecad_status()"])
        result = bval

        step_path = result.get("step_path", out_path)
        summary   = result.get("summary", {})
        _ECAD["out_dir"] = os.path.dirname(step_path)
        # Persist the aicad.boardphys/1 sidecar next to the STEP.
        boardphys = result.get("boardphys")
        sidecar_path = None
        sidecar_warnings: list = []
        if boardphys:
            sidecar_path = step_path + ".boardphys.json"
            try:
                with open(sidecar_path, "w", encoding="utf-8") as fh:
                    json.dump(boardphys, fh, indent=1)
            except Exception as e:  # noqa: BLE001 — sidecar failure must not void the STEP
                sidecar_warnings.append(f"boardphys sidecar write failed: {e}")
                sidecar_path = None
        totals = (boardphys or {}).get("totals") or {}
        step_exists = os.path.exists(step_path)
        step_bytes = os.path.getsize(step_path) if step_exists else 0
        sidecar_bytes = (os.path.getsize(sidecar_path)
                         if sidecar_path and os.path.exists(sidecar_path) else 0)
        return _dumps({
            "ok": step_exists, "doc_id": session.doc_id, "kind": "step",
            "step_path": step_path, "exists": step_exists, "bytes": step_bytes,
            "boardphys_path": sidecar_path, "boardphys_bytes": sidecar_bytes,
            "power": totals or None,
            "board_volume_mm3": summary.get("board_volume", 0.0),
            "n_components": summary.get("n_components", 0),
            "n_proxies": summary.get("n_proxies", 0),
            "summary": summary,
            "warnings": (list(summary.get("warnings", [])) + sidecar_warnings
                        + ([] if step_exists else [f"STEP write did not land at {step_path}"])),
            "hint": (f"Wrote STEP to {step_path} ({step_bytes} bytes, confirmed on disk; "
                     f"board volume {summary.get('board_volume', 0):.1f} mm³, "
                     f"{summary.get('n_components', 0)} component body/bodies)"
                     + (f" + boardphys sidecar {sidecar_path} ({sidecar_bytes} bytes, "
                        "power/thermal map)."
                        if sidecar_path else ".")
                     + " Open this STEP in the CAD app as an import feature to design "
                       "the enclosure."),
            "next_actions": [
                f"# In the CAD app: File > Import > {step_path}",
                "# Or drag the STEP onto the 3D canvas to start the enclosure design.",
            ],
        })



    # ---------------------------------------------------------------- SI rule pack

    @mcp.tool()
    def ecad_set_net_class(name: str, params: Optional[Union[dict, str]] = None,
                           nets: Optional[Union[list, str]] = None,
                           doc_id: str = "") -> str:
        """Declare/extend a net class on the active board and assign nets to it — THE
        declaration path for high-speed SI rules (match groups, termination policy, edge rate).

        ``params`` keys merge ADDITIVELY into ``board['net_classes'][name]`` — the same schema
        ecad_stackup_solve writes (track_width / diff_gap / via dims / clearance), extended with
        the SI keys ecad_run_sicheck reads:

          match_tolerance_mm  the class IS a length-match group (e.g. a DDR byte lane): every
                              assigned net's routed length must be within this of the group median
          pair_skew_mm        intra-pair skew limit; diff pairs inferred from _P/_N, +/-, P/N
          termination         {"scheme": "series"|"parallel"|"thevenin"|"none",
                               "value_ohm": 33, "max_dist_mm": 10, "driver_ref": "U1"}
          rise_time_ps        signal edge rate — enables the via-stub length check

        ``nets`` is the list of net names to assign to the class (created if new).

        Example — declare a DDR byte lane:
          ecad_set_net_class('ddr_byte0', params={'match_tolerance_mm': 0.5},
                             nets=['DQ0','DQ1','DQ2','DQ3','DQ4','DQ5','DQ6','DQ7','DQS_P','DQS_N'])
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a board first.",
                        next_actions=["ecad_new_pcb('myboard')"])
        if isinstance(params, str):
            # LIVE CRASH class: a python-repr params string ("{'match_tolerance_mm': 0.5}")
            # would json.loads-fail even though it IS the dict it looks like.
            coerced = coerce_json_or_repr(params)
            if isinstance(coerced, dict):
                params = coerced
            else:
                try:
                    params = json.loads(params)
                except Exception:
                    return _err(f"params is not valid JSON: {params!r}")
        if isinstance(nets, str):
            coerced = coerce_json_or_repr(nets)
            if isinstance(coerced, list):
                nets = coerced
            else:
                try:
                    nets = json.loads(nets) if nets.strip().startswith("[") else \
                        [n.strip() for n in nets.split(",") if n.strip()]
                except Exception:
                    return _err(f"nets is not a valid list: {nets!r}")
        try:
            import copy as _copy
            from services.ecad import si_rules as si_mod  # STAYS: no C++ counterpart
            new_doc = _copy.deepcopy(session.doc)
            res = si_mod.set_net_class(new_doc, str(name), params or {}, nets or [])
            ecad_session.swap_doc(session, new_doc)
        except Exception as e:  # noqa: BLE001
            return _err(f"set_net_class failed: {e}", hint="Inspect with ecad_status().")
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "class": res["class"], "params": res["params"], "assigned": res["assigned"],
            "warnings": [],
            "hint": f"net class {name!r} now has {len(res['params'])} key(s); "
                    f"{len(res['assigned'])} net(s) assigned.",
            "next_actions": ["ecad_run_sicheck()  # verify the declared SI rules",
                             "ecad_autoroute()  # routes honour track_width/diff_gap"],
        })



    # ---------------------------------------------------------------- stackup advisor

    @mcp.tool()
    def ecad_stackup_solve(copper_layers: int = 4,
                           targets: Optional[Union[dict, str]] = None,
                           profile: str = "auto",
                           net_map: Optional[Union[dict, str]] = None,
                           apply: bool = True,
                           doc_id: str = "") -> str:
        """Solve a controlled-impedance layer stackup + the trace geometries that hit each
        impedance target, from a fab-capability profile — then wire the result into the active
        board so autoroute and DRC honour it.

        ``targets`` maps net-class names to impedance targets: a bare number means single-ended
        ohms; a dict allows differential pairs. Default (omitted):
        ``{"se_50": 50, "diff_100": {"type": "diff", "z0": 100}}``.
        Example: ``{"clk": 50, "usb": {"type": "diff", "z0": 90}}``.

        ``profile`` picks the fab menu (canned data files): 'jlc_4layer_7628' (validated against
        JLCPCB's published JLC04161H-7628 stackup), 'jlc_6layer_3313', 'generic_2layer', or
        'auto' (first profile matching ``copper_layers``).

        ``net_map`` optionally assigns existing nets to the solved classes, e.g.
        ``{"usb": ["USB_DP", "USB_DN"]}`` — those nets then route at the solved width/gap.

        With ``apply=True`` and an active board, the solved stackup replaces the board stackup
        and each target becomes a net class carrying the solved ``track_width`` (+ ``diff_gap``)
        — exactly what ecad_autoroute and ecad_run_drc read. With no board open the solve is
        still returned (design-ahead mode).

        The solver inverts the SAME validated IPC-2141A models ecad_check_impedance uses
        (microstrip on outer layers, stripline inner), so a solved geometry reads back at target.
        Infeasible targets fail honestly naming the binding constraint (min_trace / max_trace /
        min_space).
        """
        try:
            from services.ecad import stackup as stackup_mod  # STAYS: no C++ counterpart; lazy import
        except ImportError as e:  # noqa: BLE001
            return _err(f"stackup engine unavailable ({e})",
                        hint="engine.ecad.stackup could not be imported.",
                        next_actions=["ecad_status()"])

        # --- parse the loose inputs (weak-model friendly: accept JSON strings) ----
        if isinstance(targets, str) and targets.strip():
            try:
                targets = json.loads(targets)
            except json.JSONDecodeError as e:
                return _err(f"targets is not valid JSON: {e}",
                            hint='Pass e.g. {"clk": 50, "usb": {"type": "diff", "z0": 90}}.',
                            next_actions=["ecad_stackup_solve(targets={'se_50': 50})"])
        if not targets:
            targets = {"se_50": 50.0, "diff_100": {"type": "diff", "z0": 100.0}}
        if isinstance(net_map, str) and net_map.strip():
            try:
                net_map = json.loads(net_map)
            except json.JSONDecodeError as e:
                return _err(f"net_map is not valid JSON: {e}",
                            hint='Pass e.g. {"usb": ["USB_DP", "USB_DN"]}.')

        # --- solve -----------------------------------------------------------------
        try:
            sol = stackup_mod.solve_stackup(copper_layers, targets, profile=profile)
        except stackup_mod.StackupInfeasible as e:
            return _err(str(e),
                        hint=f"Binding constraint: {e.binding}. Relax the target, pick a "
                             "profile with a thinner outer dielectric, or accept the nearest "
                             "feasible impedance.",
                        next_actions=[
                            "ecad_stackup_solve(copper_layers=4, "
                            "profile='jlc_4layer_7628')  # thin 7628 outer dielectric",
                            "ecad_check_impedance()  # see what the current geometry gives",
                        ])
        except ValueError as e:
            return _err(str(e),
                        hint="Available fab profiles: "
                             f"{', '.join(stackup_mod.list_profiles())}.",
                        next_actions=["ecad_stackup_solve(copper_layers=4, profile='auto')"])

        # --- narrate the solve -------------------------------------------------------
        lines: list[str] = []
        for name, geo in sol["geometries"].items():
            g0 = geo["layers"][sol["signal_layers"][0]]
            if geo["type"] == "diff":
                lines.append(
                    f"{name}: {geo['z0_target']:.0f} ohm differential -> "
                    f"w={geo['track_width']:.3f} mm, gap={geo['diff_gap']:.3f} mm "
                    f"({g0['kind']} on {sol['signal_layers'][0]}, h={g0['h']:.4f} mm)")
            else:
                lines.append(
                    f"{name}: {geo['z0_target']:.0f} ohm single-ended -> "
                    f"w={geo['track_width']:.3f} mm "
                    f"({g0['kind']} on {sol['signal_layers'][0]}, h={g0['h']:.4f} mm)")
        reading = (f"Stackup '{sol['profile']}' ({copper_layers} layers, "
                   f"{sol['stackup']['thickness']:.2f} mm): " + "; ".join(lines))

        # --- wire into the active board -----------------------------------------------
        applied = False
        wired: dict = {}
        session = None
        if apply:
            try:
                session = _get_pcb(doc_id or None)
            except OpError:
                session = None
        if session is not None:
            import copy as _copy
            new_doc = _copy.deepcopy(session.doc)
            try:
                wired = stackup_mod.apply_solution(new_doc, sol, net_map=net_map or None)
                ecad_session.swap_doc(session, new_doc)
                applied = True
            except (ValueError, OpError) as e:
                return _err(f"solved OK but could not apply to the board: {e}",
                            hint="The solve result is valid; fix the net_map / board and retry.",
                            next_actions=["ecad_status()"])

        if applied:
            hint = (reading + " Applied to the board: stackup replaced and net classes "
                    "updated — ecad_autoroute()/ecad_run_drc() now use the solved widths.")
            next_actions = ["ecad_autoroute()  # routes at the solved widths/gaps",
                            "ecad_check_impedance()  # verify routed traces read back at target",
                            "ecad_run_drc()"]
        else:
            hint = (reading + " No active board (or apply=False) — solve returned for "
                    "design-ahead; open a board and re-run with apply=True to wire it in.")
            next_actions = ["ecad_new_pcb('myboard')",
                            f"ecad_stackup_solve(copper_layers={copper_layers}, "
                            f"profile='{sol['profile']}')"]

        return _dumps({
            "ok": True,
            "doc_id": session.doc_id if session is not None else None,
            "applied": applied,
            "profile": sol["profile"],
            "stackup": sol["stackup"],
            "layer_roles": sol["layer_roles"],
            "signal_layers": sol["signal_layers"],
            "geometries": sol["geometries"],
            "net_classes_written": wired.get("classes", []),
            "nets_assigned": wired.get("assigned", {}),
            "warnings": [],
            "reading": reading,
            "hint": hint,
            "next_actions": next_actions,
        })



    # ---------------------------------------------------------------- one-shot board flow

    @mcp.tool()
    def ecad_make_board(pcb_name: str = "Board1", doc_id: str = "",
                        hostcheck: bool = False) -> str:
        """ONE-SHOT board flow: from the active schematic, run sync -> autoroute -> fill zones ->
        DRC and report every step's outcome, remaining issues, and what to do next.

        Steps in order:
          1. ecad_new_pcb          -- create a fresh board document
          2. ecad_update_from_sch  -- populate footprints from the schematic
          3. ecad_autoroute        -- route the ratsnest with the 2-layer router
          4. ecad_fill_zones       -- pour copper on GND (adds a zone automatically if none)
          5. ecad_run_drc          -- report remaining violations
          6. (opt-in) hostcheck=True also runs the MCU host-board rule pack
             (ecad_run_hostcheck: decoupling / bulk / sequencing / crystal / reset /
             boot-strap) and folds its verdict into the step report

        Sets _ECAD['current_pcb'] to the produced board so ecad_status / ecad_run_drc /
        ecad_export work immediately after.

        ``doc_id`` selects WHICH schematic to build the board from (defaults to the active
        schematic). Pass it when you have several schematics open and want a board from a specific
        one (Bug E5: this argument was previously ignored).

        Finishes with a hint: "call ecad_to_3d to bring the board into the CAD app for a housing".
        """
        sch_id = doc_id or _ECAD.get("current_sch")
        if sch_id is None:
            return _err("no active schematic -- build one first",
                        hint="Create a schematic (ecad_new_schematic + place + wire parts) before "
                             "calling ecad_make_board.",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_design('your circuit description')"])
        # Validate the requested schematic resolves and IS a schematic (a clear error beats
        # silently building an empty board from a bad/board doc_id).
        try:
            sch_sess = ecad_session.get_session(sch_id)
            if sch_sess.kind != "sch":
                return _err(f"doc '{sch_id}' is a {sch_sess.kind}, not a schematic",
                            hint="Pass the doc_id of a SCHEMATIC to build the board from.",
                            next_actions=["ecad_status()"])
        except OpError as e:
            return _err(e.message, hint="That schematic doc_id is not open.",
                        next_actions=["ecad_new_schematic('mydesign')"])

        steps: list[dict] = []

        def _step(name: str, outcome: dict, note: str = "") -> None:
            steps.append({
                "step": name,
                "ok": outcome.get("ok", False),
                "note": note or outcome.get("hint", "")[:120],
            })

        # ---- 1. new board (instant — kept OUTSIDE the budget so a timeout below can still report
        # the created board's id + partial steps for recovery) ----
        pcb_sess = ecad_session.new_session("pcb", pcb_name)
        _ECAD["current_pcb"] = pcb_sess.doc_id
        _ECAD["current"] = pcb_sess.doc_id
        _step("new_pcb", {"ok": True}, f"board '{pcb_name}' created ({pcb_sess.doc_id})")

        # The full sync->route->fill->DRC flow is the heaviest ECAD entry point — it was the
        # original transport-timeout trigger (adb46f62 fixed the 100x100 GND-pour symptom; this
        # bounds the whole flow). Run it under ONE wall-clock budget so its TOTAL time can never
        # overrun the transport and get the server killed (per-substep budgets wouldn't bound the
        # sum). Autosave first so even a hard kill keeps the work (Gap (a)+(b)).
        _autosave_all_open_ecad()

        def _flow() -> str:
            # ---- 2. sync schematic -> board ----
            sync_ok = False
            try:
                sync_resp = _update_from_sch(pcb_sess, ecad_session.get_session(sch_id))
                pcb_sess._update(sync_resp)  # keep pcb_sess current after the swap
                sync_rep = sync_resp.get("report", {})
                added = sync_rep.get("added", [])
                ratsnest = sync_resp.get("compile", {}).get("ratsnest", []) or []
                sync_ok = True
                _step("update_from_sch", {"ok": True},
                      f"{len(added)} footprint(s) placed, {len(ratsnest)} airwire(s) to route")
            except OpError as e:
                _step("update_from_sch", {"ok": False}, f"FAILED: {e.message}")
                added = []; ratsnest = []

            # ---- 3. autoroute ----
            ar_ok = False
            try:
                ar_resp = _rpc("ecad_autoroute",
                               {"doc_id": pcb_sess.doc_id, "opts": {}})
                pcb_sess._update(ar_resp)  # keep pcb_sess current after the RPC mutation
                ar_rep = ar_resp.get("report", {})
                routed = ar_rep.get("routed", [])
                failed = ar_rep.get("failed", [])
                ar_ok = True
                _step("autoroute", {"ok": True},
                      f"routed {len(routed)} net(s), {len(failed)} failed"
                      + (f": {failed[:4]}" if failed else ""))
            except OpError as e:
                _step("autoroute", {"ok": False}, f"FAILED: {e.message}")
                routed = []; failed = []

            # M4: compute the true routed-net count = multi-pad nets with NO remaining airwires.
            # The autorouter's ``routed`` list only includes nets that HAD airwires and the
            # router successfully connected them; single-pad stubs and zone-only nets are never
            # in the list.  We count all board nets that are NOT in ``failed`` (unrouted) and
            # have at least 2 pads (so single-pad stubs don't inflate the "routed" figure).
            try:
                _pcb_doc_now = pcb_sess.doc
                _all_board_nets = {
                    n.get("name") for n in _pcb_doc_now.get("nets", [])
                    if isinstance(n, dict) and n.get("name")
                }
                # count pads per net
                from collections import Counter as _Counter
                _pad_net_counts: _Counter = _Counter()
                for _fp in _pcb_doc_now.get("footprints", []):
                    for _pad in _fp.get("pads", []):
                        _pn = _pad.get("net")
                        if _pn:
                            _pad_net_counts[_pn] += 1
                # a net is "multi-pad" when it has >= 2 pads (i.e. needs a connection)
                _multi_pad_nets = {n for n, c in _pad_net_counts.items() if c >= 2}
                _failed_set = set(failed)
                _nets_routed_count = len(_multi_pad_nets - _failed_set)
                _nets_failed_count = len(_multi_pad_nets & _failed_set)
            except Exception:  # noqa: BLE001
                _nets_routed_count = len(routed)
                _nets_failed_count = len(failed)

            # ---- 3.5 board outline (auto Edge.Cuts rectangle if none) ----
            # The one-shot flow must leave a closed Edge.Cuts loop: ecad_to_3d needs it to
            # build the substrate, and the GND pour wants the true board extents (Bug E-3).
            outline_ok = False
            try:
                pcb_doc = pcb_sess.doc
                if not (pcb_doc.get("outline", {}) or {}).get("shapes"):
                    shapes = _default_outline_shapes(pcb_doc)
                    _apply(pcb_sess, [{"op": "pcb_set_outline", "shapes": shapes, "closed": True}])
                    bb = _outline_bbox(pcb_sess.doc)
                    outline_ok = True
                    _step("set_outline", {"ok": True},
                          "auto Edge.Cuts rectangle "
                          + (f"{bb[2]-bb[0]:.1f} x {bb[3]-bb[1]:.1f} mm" if bb else "added"))
                else:
                    outline_ok = True
                    _step("set_outline", {"ok": True}, "outline already present — kept")
            except Exception as e:  # noqa: BLE001
                _step("set_outline", {"ok": False}, f"FAILED: {type(e).__name__}: {e}")

            # ---- 4. fill zones (add GND zone automatically if board has none) ----
            fill_ok = False
            try:
                ecad_session.tree(pcb_sess)  # refresh: sync+autoroute advanced the C++ doc
                pcb_doc = pcb_sess.doc
                if not pcb_doc.get("zones"):
                    # add a whole-board GND zone (need a bounding box for the polygon).
                    poly = _outline_bbox(pcb_doc)
                    if poly is None:
                        # No board outline: size the pour to the actual COPPER extent (placed
                        # footprints + routed tracks) plus a margin — NOT a fixed 100x100 mm. A
                        # 100x100 zone filled at 0.1 mm resolution is ~1e6 cells (~5 s); for a small
                        # board that overran the MCP transport timeout in this SYNCHRONOUS flow, the
                        # server was killed, and every ECAD doc (Python, no autosave) was lost. The
                        # tight extent fills in ~0.1 s. (_board_bbox already returns the copper
                        # extent + a small pad when there is no outline.)
                        bx0, by0, bx1, by1 = _board_bbox(pcb_doc)
                        m = 5.0  # pour margin beyond the copper
                        poly = (bx0 - m, by0 - m, bx1 + m, by1 + m)
                    x0, y0, x1, y1 = poly
                    poly_pts = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
                    _apply(pcb_sess, [{"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
                                       "polygon": poly_pts}])
                fill_resp = _rpc("ecad_fill_zones", {"doc_id": pcb_sess.doc_id})
                fs = fill_resp.get("fill_summary", {})
                fill_ok = True
                _step("fill_zones", {"ok": True},
                      f"{fs.get('zones_filled', 0)} zone(s) filled, {fs.get('copper_area_mm2', 0):.1f} mm^2")
            except Exception as e:  # noqa: BLE001
                _step("fill_zones", {"ok": False}, f"FAILED: {type(e).__name__}: {e}")

            # ---- 5. DRC ----
            drc_errors = 0; drc_warnings = 0; drc_lines: list[str] = []
            try:
                violations = _compile_of(pcb_sess).get("drc", [])
                if isinstance(violations, dict):
                    violations = violations.get("violations", [])
                drc_sum = _erc_summary(violations)
                drc_errors = drc_sum["errors"]
                drc_warnings = drc_sum["warnings"]
                drc_lines = _erc_lines(violations)[:6]
                _step("drc", {"ok": drc_errors == 0},
                      f"{drc_errors} error(s), {drc_warnings} warning(s)")
            except Exception as e:  # noqa: BLE001
                _step("drc", {"ok": False}, f"DRC unavailable: {e}")

            # ---- 6. host-board rule pack (opt-in) ----
            hb_errors = 0; hb_warnings = 0; hb_lines: list[str] = []
            if hostcheck:
                try:
                    from services.ecad import hostboard_rules as hb_mod  # STAYS: no C++ counterpart
                    hb_v = hb_mod.run_hostcheck(pcb_sess.doc)
                    hb_sum = _erc_summary(hb_v)
                    hb_errors = hb_sum["errors"]
                    hb_warnings = hb_sum["warnings"]
                    hb_lines = _erc_lines(hb_v)[:6]
                    _step("hostcheck", {"ok": hb_errors == 0},
                          f"{hb_errors} error(s), {hb_warnings} warning(s)")
                except Exception as e:  # noqa: BLE001
                    _step("hostcheck", {"ok": False}, f"hostcheck unavailable: {e}")

            all_ok = all(s["ok"] for s in steps)
            summary_lines = [f"  {s['step']}: {'OK' if s['ok'] else 'FAIL'} -- {s['note']}"
                             for s in steps]
            reading = "\n".join(summary_lines)
            hint = (f"Board flow {'complete' if all_ok else 'partial'}: "
                    + "; ".join(f"{s['step']}={'OK' if s['ok'] else 'FAIL'}" for s in steps)
                    + f". DRC: {drc_errors} error(s). "
                    "Next: call ecad_to_3d() to bring the board into the CAD app for a housing.")

            next_actions: list[str] = []
            if failed:
                next_actions.append(
                    f"{len(failed)} net(s) not routed ({', '.join(map(str, failed[:4]))}). "
                    "Try ecad_autoroute(pitch=0.15) or move parts to give the router more room.")
            if drc_errors:
                next_actions.append(f"ecad_run_drc()  # {drc_errors} DRC error(s) to fix")
            if hostcheck and hb_errors:
                next_actions.append(
                    f"ecad_run_hostcheck()  # {hb_errors} host-board rule error(s) — "
                    "see fix_hints")
            next_actions.append("ecad_to_3d()  # export STEP and design an enclosure in the CAD app")
            if not next_actions:
                next_actions.append("ecad_export('gerber')  # board passes DRC -- ready for fab")

            return _dumps({
                "ok": all_ok, "doc_id": pcb_sess.doc_id,
                "sch_doc_id": sch_id, "pcb_name": pcb_name,
                "steps": steps, "reading": reading,
                "footprints_placed": len(added),
                # M4: nets_routed = multi-pad nets with no remaining airwires (all connected).
                # nets_autorouted = nets the router actively connected in this run (subset).
                # nets_failed = multi-pad nets that still have unrouted airwires.
                "nets_routed": _nets_routed_count,
                "nets_autorouted": len(routed),
                "nets_failed": _nets_failed_count,
                "drc_errors": drc_errors, "drc_warnings": drc_warnings,
                "drc_violations": drc_lines,
                **({"hostcheck_errors": hb_errors, "hostcheck_warnings": hb_warnings,
                    "hostcheck_violations": hb_lines} if hostcheck else {}),
                "warnings": drc_lines[:3],
                "hint": hint,
                "next_actions": next_actions,
            })

        status, out = _run_with_budget(_flow)
        if status == "timeout":
            return _budget_timeout_err(
                "make_board flow",
                simplify="The one-shot sync->route->fill->DRC flow is too heavy for a single call. "
                         "Run the steps separately so each is individually budget-guarded: "
                         "ecad_update_from_sch(), then ecad_autoroute(pitch=0.3), then "
                         "ecad_fill_zones(), then ecad_run_drc().",
                next_actions=["ecad_autoroute(pitch=0.3)  # on the partial board",
                              "ecad_fill_zones()", "ecad_run_drc()"],
                doc_id=pcb_sess.doc_id,
                steps_completed=[s["step"] for s in steps])
        if status == "error":
            raise out
        return out



    # ---------------------------------------------------------------- board flow (Wave 2b)
    # Deterministic schematic->preliminary-PCB + standalone place/optimize tools.
    # Engine lives in services.mcp.board_flow (reuses E1 place_board + E2 optimize_traces).

    @mcp.tool()
    def board_from_schematic(sch_doc_id: str = "", optimize: bool = True,
                             doc_id: str = "",
                             board_size: Optional[Union[list, str]] = None,
                             outline_margin: float = 0.0,
                             outline_area_scale: float = 0.0,
                             outline: Optional[Union[list, str]] = None,
                             force_outline: bool = False) -> str:
        """ONE-CALL schematic -> preliminary PCB: sync footprints, place (connectivity-aware),
        autoroute, then (by default) optimize traces (arc corners / teardrops / impedance).

        Takes the active schematic (or ``sch_doc_id``) and emits a placed + pre-routed board.
        Reuses the E1 placer and E2 trace optimizer — no duplicate layout engine. Deterministic
        for a fixed seed (default 0): re-running on an unchanged schematic yields the same
        placement + routing.

        Residual DRC violations are listed honestly in ``residual_drc`` (never hidden). Use
        ``optimize=False`` to skip the post-route polish pass. ``doc_id`` is accepted as an
        alias for ``sch_doc_id`` (same meaning as on other ecad tools).

        mega-27 outline room knobs (optional; defaults keep prior AUTO-OUTLINE behaviour)::

          * ``board_size`` ``[w, h]`` mm — placeable area + Edge.Cuts rectangle size
          * ``outline_margin`` mm — Edge.Cuts pad beyond parts (default engine 5.0)
          * ``outline_area_scale`` — expand outline area about center (>=1)
          * ``outline`` ``[[x,y],...]`` — explicit closed polygon Edge.Cuts
          * ``force_outline`` — replace an existing outline (outline-grow retry)

        Typical agent path: ecad_design / capture schematic -> ecad_simulate (gate) ->
        board_from_schematic -> ecad_run_drc / ecad_run_dfm / ecad_run_pdn / thermal checks.

        AUTO-READBACK: compact board ASCII + net_delta + findings. READ it before further
        place/route. Call render_board_ascii before declaring the board done.
        """
        from services.mcp import board_flow as _bf

        sch_id = sch_doc_id or doc_id or _ECAD.get("current_sch")
        if not sch_id:
            return _err(
                "no active schematic for board_from_schematic",
                hint="Build or open a schematic first (ecad_new_schematic / ecad_design), "
                     "or pass sch_doc_id=...",
                next_actions=["ecad_design('your circuit')",
                              "ecad_new_schematic('mydesign')"],
            )
        try:
            sch_sess = ecad_session.get_session(str(sch_id))
            if sch_sess.kind != "sch":
                return _err(
                    f"doc '{sch_id}' is a {sch_sess.kind}, not a schematic",
                    hint="Pass the doc_id of a SCHEMATIC.",
                    next_actions=["ecad_status()"],
                )
        except OpError as e:
            return _err(e.message, hint="That schematic doc_id is not open.",
                        next_actions=["ecad_new_schematic('mydesign')"])

        _autosave_all_open_ecad()

        flow_opts: dict = {
            "optimize": bool(optimize),
            "seed": 0,
            "pcb_name": "Board1",
        }
        # SILENT-DISCARD guard: board_size/outline used to be checked with a bare
        # `isinstance(x, list)` — a stringified "[100, 80]"/python-repr value quietly
        # fell through BOTH `if` branches below with no error, so the caller's explicit
        # room knob was dropped and AUTO-OUTLINE ran instead while reporting ok=True.
        if isinstance(board_size, str) and board_size.strip():
            board_size = coerce_json_or_repr(board_size)
            if isinstance(board_size, str):
                return _err(f"board_size is not a valid [w, h] list: {board_size!r}",
                            hint="Pass board_size=[100, 80] (mm).")
        if board_size is not None and isinstance(board_size, (list, tuple)) and len(board_size) >= 2:
            flow_opts["board_size"] = [float(board_size[0]), float(board_size[1])]
        elif board_size is not None:
            return _err(f"board_size must be a [w, h] list, got {board_size!r}",
                        hint="Pass board_size=[100, 80] (mm).")
        if outline_margin and float(outline_margin) > 0:
            flow_opts["outline_margin"] = float(outline_margin)
        if outline_area_scale and float(outline_area_scale) > 0:
            flow_opts["outline_area_scale"] = float(outline_area_scale)
        if isinstance(outline, str) and outline.strip():
            outline = coerce_json_or_repr(outline)
            if isinstance(outline, str):
                return _err(f"outline is not a valid polygon list: {outline!r}",
                            hint="Pass outline=[[x,y], ...] (>=3 points).")
        if outline is not None and isinstance(outline, list) and len(outline) >= 3:
            flow_opts["outline"] = outline
        elif outline is not None:
            return _err(f"outline must be a [[x,y],...] list (>=3 points), got {outline!r}",
                        hint="Pass outline=[[x,y], ...] (>=3 points).")
        if force_outline:
            flow_opts["force_outline"] = True

        def _flow() -> dict:
            return _bf.board_from_schematic(sch_sess, flow_opts)

        status, result = _run_with_budget(_flow)
        if status == "timeout":
            return _budget_timeout_err(
                "board_from_schematic",
                simplify="The preliminary board flow is too heavy for one call. Run steps "
                         "separately: ecad_update_from_sch(), ecad_autoplace(), "
                         "ecad_autoroute(pitch=0.3), ecad_optimize_traces().",
                next_actions=[
                    "ecad_update_from_sch()",
                    "ecad_autoplace()",
                    "ecad_autoroute(pitch=0.3)",
                    "ecad_optimize_traces()",
                ],
            )
        if status == "error":
            e = result
            if isinstance(e, OpError):
                return _err(f"board_from_schematic failed: {e.message}",
                            hint=e.message,
                            next_actions=["ecad_status()"],
                            detail=getattr(e, "detail", {}))
            raise e

        board_id = result.get("board_doc_id")
        if board_id:
            _ECAD["current_pcb"] = board_id
            _ECAD["current"] = board_id

        rep = result.get("report") or {}
        residual = result.get("residual_drc") or []
        n_err = sum(1 for v in residual if v.get("severity") == "error")
        failed = result.get("failed_nets") or rep.get("failed_nets") or []
        placed = int(result.get("placed") or 0)
        routed = int(result.get("routed") or 0)
        corners = int(rep.get("corners_rounded") or 0)
        tears = int(rep.get("teardrops") or 0)
        tuned = int(rep.get("nets_tuned") or 0)
        rn_before = rep.get("ratsnest_before", 0)
        rn_after = rep.get("ratsnest_after", 0)

        residual_lines = [
            f"[{v.get('severity', '?')}] {v.get('rule', '?')}: {v.get('message', '')}"
            for v in residual[:8]
        ]
        head = (
            f"preliminary board: {placed} footprint(s), routed={routed}, "
            f"ratsnest {rn_before}->{rn_after}, "
            f"opt corners={corners}/teardrops={tears}/z0={tuned}; "
            f"DRC residual errors={n_err}."
        )
        if failed:
            head += f" Unrouted nets: {', '.join(map(str, failed[:6]))}."

        next_actions: list[str] = []
        if failed:
            next_actions.append(
                f"ecad_autoroute(pitch=0.15)  # retry unrouted: "
                f"{', '.join(map(str, failed[:4]))}")
        if n_err:
            next_actions.append(
                f"ecad_run_drc()  # {n_err} residual DRC error(s) named in residual_drc")
        if not optimize:
            next_actions.append("ecad_optimize_traces()  # arc/teardrop/impedance polish")
        next_actions.append("ecad_to_3d()  # STEP + enclosure design")
        if not next_actions:
            next_actions.append("ecad_export('gerber')  # board ready for fab review")

        payload = {
            "ok": bool(result.get("ok", True)),
            "doc_id": board_id,
            "board_doc_id": board_id,
            "sch_doc_id": result.get("sch_doc_id") or sch_id,
            "revision": result.get("revision"),
            "placed": placed,
            "routed": routed,
            "routed_nets": result.get("routed_nets") or [],
            "failed_nets": failed,
            "residual_drc": residual,
            "residual_drc_lines": residual_lines,
            "drc_errors": n_err,
            # mega-27 C27 evidence: outline room vs component bbox
            "outline_area_mm2": result.get("outline_area_mm2") or rep.get("outline_area_mm2"),
            "component_bbox_area_mm2": (
                result.get("component_bbox_area_mm2")
                or rep.get("component_bbox_area_mm2")
            ),
            "outline_size_mm": result.get("outline_size_mm") or rep.get("outline_size_mm"),
            "report": {
                "ratsnest_before": rn_before,
                "ratsnest_after": rn_after,
                "corners_rounded": corners,
                "teardrops": tears,
                "nets_tuned": tuned,
                "drc_errors": n_err,
                "seed": rep.get("seed", 0),
                "steps": rep.get("steps") or [],
                "outline_area_mm2": result.get("outline_area_mm2") or rep.get("outline_area_mm2"),
                "component_bbox_area_mm2": (
                    result.get("component_bbox_area_mm2")
                    or rep.get("component_bbox_area_mm2")
                ),
                "outline_size_mm": result.get("outline_size_mm") or rep.get("outline_size_mm"),
            },
            "warnings": list(result.get("warnings") or []) + residual_lines[:3],
            "summary": head,
            "hint": head + (" All multi-pad nets routed." if not failed else
                            " Some nets need manual routing or more space.")
                    + " READ the ascii_readback before the next board edit.",
            "next_actions": next_actions,
        }
        if board_id:
            _attach_board_ascii_readback(payload, doc_id=board_id)
        return _dumps(payload)



    @mcp.tool()
    def ecad_autoplace(doc_id: str = "") -> str:
        """Connectivity-aware placement on the active board (E1 placer).

        Seats footprints by net clusters: ICs near centre, decoupling caps next to IC power
        pads, connectors on the edge, passives near their highest-weight neighbour. Deterministic
        (seed=0). Run after ecad_update_from_sch (or board_from_schematic already did this).

        AUTO-READBACK: compact layer ASCII + courtyard/net findings. READ before routing.
        Call render_board_ascii before declaring the board done.
        """
        from services.mcp import board_flow as _bf

        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Autoplace needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')",
                                      "board_from_schematic()"])

        try:
            result = _bf.autoplace_board(session, {"seed": 0})
        except OpError as e:
            return _err(f"ecad_autoplace failed: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))

        rep = result.get("report") or {}
        residual = result.get("residual_drc") or []
        n_err = sum(1 for v in residual if v.get("severity") == "error")
        moved = int(result.get("moved") or rep.get("moved") or 0)
        n_fp = int(result.get("placed") or rep.get("n_footprints") or 0)
        rn = rep.get("ratsnest_len_mm", 0.0)
        head = (f"autoplace: moved {moved}/{n_fp} footprint(s); "
                f"ratsnest_len={float(rn):.2f} mm; overlaps={rep.get('overlaps', 0)}; "
                f"DRC residual errors={n_err}.")
        next_actions = [
            "ecad_autoroute()  # route the ratsnest after placement",
            "ecad_optimize_traces()  # post-route polish",
        ]
        if n_err:
            next_actions.insert(0, f"ecad_run_drc()  # {n_err} residual error(s)")
        payload = {
            "ok": True,
            "doc_id": session.doc_id,
            "board_doc_id": session.doc_id,
            "revision": result.get("revision") or session.revision,
            "placed": n_fp,
            "moved": moved,
            "report": {
                "ratsnest_len_mm": rn,
                "moved": moved,
                "overlaps": rep.get("overlaps", 0),
                "n_footprints": n_fp,
                "seed": rep.get("seed", 0),
            },
            "residual_drc": residual,
            "drc_errors": n_err,
            "warnings": list(result.get("warnings") or []),
            "summary": head,
            "hint": head + " Next: ecad_autoroute then ecad_optimize_traces. "
                    "READ the ascii_readback before the next board edit.",
            "next_actions": next_actions,
        }
        _attach_board_ascii_readback(payload, doc_id=session.doc_id)
        return _dumps(payload)



    @mcp.tool()
    def ecad_optimize_traces(targets: Optional[Union[dict, str]] = None,
                             doc_id: str = "") -> str:
        """Post-route polish on the active board (E2): round ~90° corners to arcs, add
        teardrops at pad/via junctions, and retune track widths toward impedance targets.

        ``targets`` is an optional ``{net_name: z0_ohm}`` map (or a JSON object string).
        DRC-guarded: each change that introduces a clearance error is reverted. Residual
        violations after the pass are listed honestly. Safe to re-run (idempotent).

        AUTO-READBACK: compact layer ASCII + net_delta after polish. READ before fill/DRC.
        Call render_board_ascii before declaring the board done.
        """
        from services.mcp import board_flow as _bf

        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Trace optimization needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')",
                                      "board_from_schematic()"])

        opt_opts: dict = {}
        if targets is not None:
            if isinstance(targets, str):
                if targets.strip():
                    # LIVE CRASH class: a python-repr targets string ("{'CLK': 50}")
                    # would json.loads-fail even though it IS the map it looks like.
                    parsed = coerce_json_or_repr(targets)
                    if isinstance(parsed, str):
                        try:
                            parsed = json.loads(parsed)
                        except (TypeError, ValueError) as e:
                            return _err(
                                f"targets is not valid JSON: {e}",
                                hint='Pass a dict or JSON object, e.g. \'{"CLK": 50}\'.',
                                next_actions=["ecad_optimize_traces(targets={'CLK': 50})"],
                            )
                    if not isinstance(parsed, dict):
                        return _err("targets must be a JSON object {net: z0_ohm}",
                                    hint='e.g. \'{"CLK": 50, "USB_DP": 90}\'.')
                    opt_opts["targets"] = parsed
            elif isinstance(targets, dict):
                opt_opts["targets"] = targets
            else:
                return _err("targets must be a dict or JSON object string",
                            hint='e.g. targets={"CLK": 50}')

        try:
            result = _bf.optimize_board_traces(session, opt_opts)
        except OpError as e:
            return _err(f"ecad_optimize_traces failed: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))

        residual = result.get("residual_drc") or []
        n_err = int(result.get("drc_errors") or
                    sum(1 for v in residual if v.get("severity") == "error"))
        corners = int(result.get("corners_rounded") or 0)
        tears = int(result.get("teardrops") or 0)
        tuned = int(result.get("nets_tuned") or 0)
        head = (f"optimize_traces: corners_rounded={corners}, teardrops={tears}, "
                f"nets_tuned={tuned}; DRC residual errors={n_err}.")
        residual_lines = [
            f"[{v.get('severity', '?')}] {v.get('rule', '?')}: {v.get('message', '')}"
            for v in residual[:6]
        ]
        next_actions: list[str] = []
        if n_err:
            next_actions.append(f"ecad_run_drc()  # {n_err} residual error(s)")
        next_actions.append("ecad_fill_zones()  # pour copper if needed")
        next_actions.append("ecad_to_3d()")
        payload = {
            "ok": True,
            "doc_id": session.doc_id,
            "board_doc_id": session.doc_id,
            "revision": result.get("revision") or session.revision,
            "corners_rounded": corners,
            "teardrops": tears,
            "nets_tuned": tuned,
            "report": {
                "corners_rounded": corners,
                "teardrops": tears,
                "nets_tuned": tuned,
                "drc_errors": n_err,
                "drc_ok": n_err == 0,
            },
            "residual_drc": residual,
            "residual_drc_lines": residual_lines,
            "drc_errors": n_err,
            "warnings": list(result.get("warnings") or []) + residual_lines[:3],
            "summary": head,
            "hint": head + (" Traces polished." if n_err == 0 else
                            " Residual DRC named — see residual_drc.")
                    + " READ the ascii_readback before the next board edit.",
            "next_actions": next_actions,
        }
        _attach_board_ascii_readback(payload, doc_id=session.doc_id)
        return _dumps(payload)
