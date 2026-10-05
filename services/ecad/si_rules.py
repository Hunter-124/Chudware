"""High-speed / signal-integrity rule pack — formula + heuristic checks over a board.

Advisory SI verification in the same spirit as :mod:`services.ecad.drc`: pure functions,
never mutate the board, deterministic sorted output, violations in the PCB-contract shape
(``{"rule", "severity", "locs", "items", "message"}``). Reuses the validated IPC-2141A closed
forms in :mod:`services.ecad.impedance` for every delay/impedance number.

HONESTY (stated in tool output too — see :data:`LIMITATIONS`): these are closed-form and
topological heuristics, NOT a 2-D/3-D field solver and NOT an IBIS/behavioural simulation.
They catch the classic layout mistakes (mismatched byte lanes, missing terminations, long via
stubs, unescapable BGA pads) with the same fidelity a careful reviewer's spreadsheet would —
they do not predict eye diagrams, crosstalk, or SSN.

Net-class schema extension (ADDITIVE keys on ``board['net_classes'][<name>]`` — the same
entries ``stackup.apply_solution`` writes ``track_width``/``diff_gap``/via dims into; a class
with none of these keys behaves exactly as before):

  ``match_tolerance_mm``  float — the class IS a length-match group: every net assigned to the
                          class (``board['nets'][i]['class']``) must have a routed length within
                          this tolerance of the group median. E.g. a DDR byte lane.
  ``pair_skew_mm``        float — intra-pair skew limit. Diff pairs inside the class are inferred
                          from net-name suffixes (``_P/_N``, ``P/N``, ``+/-``, ``_p/_n``).
  ``termination``         dict — required termination topology for every net in the class::
                              {"scheme": "series"|"parallel"|"thevenin"|"none",
                               "value_ohm": float|None,        # expected R (informational)
                               "max_dist_mm": float,           # series-R proximity to driver
                               "driver_ref": str|None}         # footprint ref of the driver
                          Detection is netlist-topological: a "resistor" is any footprint whose
                          ref starts with ``R`` and has exactly 2 netted pads.
  ``rise_time_ps``        float — signal edge rate; enables the via-stub check. A through-via's
                          unused barrel is a stub; we warn when the round-trip stub delay
                          exceeds ``rise_time_ps / 3`` (the common tr/3 reflection heuristic).

Rule ids emitted: ``si_match_length``, ``si_pair_skew``, ``si_termination``, ``si_via_stub``,
``si_bga_via_in_pad``, ``si_bga_escape``, ``si_bga_congestion``.
"""

from __future__ import annotations

import math
from typing import Any

from services.ecad import schema
from services.ecad.impedance import (
    _delay_ps_per_mm,
    _stackup_geometry,
    _track_length_mm,
)

# Honest statement of scope, returned verbatim by run_sicheck + the MCP tool.
LIMITATIONS: list[str] = [
    "Closed-form / heuristic checks only: IPC-2141A quasi-static impedance + geometric "
    "topology. NOT a 2-D/3-D field solver — no crosstalk, no return-path, no SSN analysis.",
    "No IBIS / behavioural driver models: termination checks are presence/topology only "
    "(a resistor footprint in the right place), not a waveform simulation.",
    "Length matching compares routed copper length; it does not model pad/package delay "
    "or serpentine coupling.",
    "Via-stub delay uses the stackup's thickness-weighted er; back-drilling is not modelled "
    "(a flagged stub may be fine if the fab back-drills it).",
]

# Diff-pair name suffix conventions, checked longest-first.
_PAIR_SUFFIXES: list[tuple[str, str]] = [
    ("_P", "_N"), ("_p", "_n"), ("+", "-"), ("P", "N"),
]

# Supply-ish / ground-ish net name heuristics for parallel / Thevenin termination rails.
_GND_NAMES = ("GND", "VSS", "AGND", "DGND", "PGND", "GNDA", "GNDD", "0")
_PWR_PREFIXES = ("VCC", "VDD", "VTT", "VREF", "V3", "V5", "V1", "3V", "5V", "1V", "2V", "VBUS",
                 "VDDQ", "PWR", "+")


def _violation(rule: str, severity: str, locs: list, items: list, message: str) -> dict:
    """Same constructor shape as drc._violation (kept local so drc stays import-light)."""
    return {
        "rule": rule,
        "severity": severity,
        "locs": [[float(x), float(y), str(layer)] for (x, y, layer) in locs],
        "items": sorted(str(i) for i in items),
        "message": message,
    }


# ────────────────────────────────────────────────────── net / class helpers


def _class_nets(board: dict) -> dict[str, list[str]]:
    """class name -> sorted list of net names assigned to it."""
    out: dict[str, list[str]] = {}
    for n in board.get("nets", []):
        if isinstance(n, dict) and n.get("name") and n.get("class"):
            out.setdefault(n["class"], []).append(n["name"])
    for k in out:
        out[k].sort()
    return out


def net_length_mm(board: dict, net: str) -> float:
    """Total routed copper length of ``net`` across ALL layers (segments + arcs)."""
    return _track_length_mm(board, net, None)


def _net_rep_loc(board: dict, net: str) -> tuple[float, float, str]:
    """A representative (x, y, layer) for a net — the midpoint of its first track, else origin."""
    for tr in board.get("tracks", []):
        if tr.get("net") == net:
            p1 = tr.get("p1", [0.0, 0.0])
            p2 = tr.get("p2", p1)
            return ((float(p1[0]) + float(p2[0])) / 2.0,
                    (float(p1[1]) + float(p2[1])) / 2.0,
                    str(tr.get("layer", "F.Cu")))
    return (0.0, 0.0, "F.Cu")


def infer_pairs(nets: list[str]) -> list[tuple[str, str]]:
    """Infer diff pairs from name suffix conventions (_P/_N, +/-, P/N). Deterministic."""
    pairs: list[tuple[str, str]] = []
    used: set[str] = set()
    for net in sorted(nets):
        if net in used:
            continue
        for sp, sn in _PAIR_SUFFIXES:
            if net.endswith(sp):
                mate = net[: -len(sp)] + sn
                if mate in nets and mate not in used and mate != net:
                    pairs.append((net, mate))
                    used.add(net)
                    used.add(mate)
                    break
    return pairs


# ────────────────────────────────────────────────────── length matching


def check_match_groups(board: dict) -> tuple[list[dict], dict[str, Any]]:
    """Inter-net length matching per declared match group (a net class with
    ``match_tolerance_mm``) + intra-pair skew (``pair_skew_mm``).

    Returns ``(violations, report)`` where report maps group name ->
    ``{tolerance_mm, reference_mm, nets: {net: {length_mm, delta_mm, ok}}, pairs: [...]}``.
    """
    out: list[dict] = []
    report: dict[str, Any] = {}
    by_class = _class_nets(board)
    classes = board.get("net_classes", {})

    for cls_name in sorted(classes):
        cls = classes[cls_name] or {}
        tol = cls.get("match_tolerance_mm")
        skew_tol = cls.get("pair_skew_mm")
        if tol is None and skew_tol is None:
            continue
        nets = by_class.get(cls_name, [])
        lengths = {net: net_length_mm(board, net) for net in nets}

        entry: dict[str, Any] = {"nets": {}, "pairs": []}
        report[cls_name] = entry

        # ── inter-net matching against the group median ──
        if tol is not None and len(nets) >= 2:
            tol = float(tol)
            vals = sorted(lengths.values())
            mid = len(vals) // 2
            ref = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0
            entry["tolerance_mm"] = tol
            entry["reference_mm"] = ref
            for net in nets:
                delta = lengths[net] - ref
                ok = abs(delta) <= tol + 1e-9
                entry["nets"][net] = {"length_mm": round(lengths[net], 4),
                                      "delta_mm": round(delta, 4), "ok": ok}
                if not ok:
                    out.append(_violation(
                        "si_match_length", "warning", [_net_rep_loc(board, net)], [net],
                        f"net {net!r} in match group {cls_name!r} is {delta:+.3f}mm vs the "
                        f"group median {ref:.3f}mm (tolerance ±{tol:.3f}mm) — lengthen/shorten "
                        f"to match"))
        elif tol is not None:
            entry["tolerance_mm"] = float(tol)
            entry["note"] = "match group has <2 assigned nets — nothing to compare"

        # ── intra-pair skew ──
        if skew_tol is not None:
            skew_tol = float(skew_tol)
            entry["pair_skew_mm"] = skew_tol
            for (p, n) in infer_pairs(nets):
                lp, ln = lengths.get(p, net_length_mm(board, p)), lengths.get(n, net_length_mm(board, n))
                skew = lp - ln
                ok = abs(skew) <= skew_tol + 1e-9
                entry["pairs"].append({"p": p, "n": n, "skew_mm": round(skew, 4), "ok": ok})
                if not ok:
                    out.append(_violation(
                        "si_pair_skew", "warning",
                        [_net_rep_loc(board, p), _net_rep_loc(board, n)], [p, n],
                        f"diff pair {p}/{n} intra-pair skew {skew:+.3f}mm exceeds "
                        f"±{skew_tol:.3f}mm (class {cls_name!r}) — equalise the two legs"))
    return out, report


# ────────────────────────────────────────────────────── termination


def _resistors(board: dict) -> list[dict]:
    """2-netted-pad footprints with ref prefix R: ``{ref, nets: (a, b), at}``. Deterministic."""
    out: list[dict] = []
    for fp in board.get("footprints", []):
        ref = str(fp.get("ref", "") or "")
        if not ref.upper().startswith("R"):
            continue
        netted = [p for p in fp.get("pads", []) if p.get("net")]
        if len(netted) != 2:
            continue
        out.append({
            "ref": ref,
            "id": fp.get("id", ""),
            "nets": (netted[0]["net"], netted[1]["net"]),
            "at": tuple(float(c) for c in fp.get("pos", [0.0, 0.0])),
        })
    out.sort(key=lambda r: r["ref"])
    return out


def _is_gnd(net: str) -> bool:
    return net.upper() in _GND_NAMES


def _is_pwr(net: str) -> bool:
    u = net.upper()
    return any(u.startswith(p) for p in _PWR_PREFIXES) and not _is_gnd(net)


def _fp_by_ref(board: dict, ref: str) -> dict | None:
    for fp in board.get("footprints", []):
        if fp.get("ref") == ref:
            return fp
    return None


def check_termination(board: dict) -> list[dict]:
    """Termination presence/topology per net class ``termination`` policy.

    * ``series``   — a 2-pin R joins the net to a companion net (the net is split around the R).
                     With ``driver_ref`` set, the R must also sit within ``max_dist_mm`` of that
                     footprint (series termination belongs at the driver).
    * ``parallel`` — a 2-pin R joins the net to a ground-ish or VTT-ish rail (end termination).
    * ``thevenin`` — TWO resistors on the net: one to a supply-ish rail, one to a ground-ish rail.
    * ``none``     — no check (explicitly unterminated, e.g. short low-speed nets).
    """
    out: list[dict] = []
    classes = board.get("net_classes", {})
    by_class = _class_nets(board)
    resistors = _resistors(board)

    for cls_name in sorted(classes):
        cls = classes[cls_name] or {}
        pol = cls.get("termination")
        if not isinstance(pol, dict):
            continue
        scheme = str(pol.get("scheme", "none")).lower()
        if scheme in ("", "none"):
            continue
        max_dist = float(pol.get("max_dist_mm", 10.0))
        driver_ref = pol.get("driver_ref")

        for net in by_class.get(cls_name, []):
            on_net = [r for r in resistors if net in r["nets"]]
            loc = _net_rep_loc(board, net)

            if scheme == "series":
                # R joins this net to a non-rail companion net (net split around the R).
                series = [r for r in on_net
                          if not _is_gnd(_other(r, net)) and not _is_pwr(_other(r, net))]
                if not series:
                    out.append(_violation(
                        "si_termination", "warning", [loc], [net],
                        f"net {net!r} (class {cls_name!r}) requires a SERIES termination "
                        f"resistor near the driver but no 2-pin R joins it to a companion "
                        f"net — add one at the source"))
                    continue
                if driver_ref:
                    drv = _fp_by_ref(board, str(driver_ref))
                    if drv is not None:
                        dpos = tuple(float(c) for c in drv.get("pos", [0.0, 0.0]))
                        best = min(math.hypot(r["at"][0] - dpos[0], r["at"][1] - dpos[1])
                                   for r in series)
                        if best > max_dist + 1e-9:
                            refs = ", ".join(r["ref"] for r in series)
                            out.append(_violation(
                                "si_termination", "warning", [loc], [net],
                                f"net {net!r}: series R ({refs}) is {best:.1f}mm from driver "
                                f"{driver_ref} (> {max_dist:.1f}mm) — move it next to the "
                                f"driver pin"))

            elif scheme == "parallel":
                ok = any(_is_gnd(_other(r, net)) or _other(r, net).upper().startswith("VTT")
                         for r in on_net)
                if not ok:
                    out.append(_violation(
                        "si_termination", "warning", [loc], [net],
                        f"net {net!r} (class {cls_name!r}) requires a PARALLEL termination "
                        f"(R to GND/VTT at the far end) but none is present"))

            elif scheme == "thevenin":
                to_pwr = any(_is_pwr(_other(r, net)) for r in on_net)
                to_gnd = any(_is_gnd(_other(r, net)) for r in on_net)
                if not (to_pwr and to_gnd):
                    missing = []
                    if not to_pwr:
                        missing.append("R to a supply rail")
                    if not to_gnd:
                        missing.append("R to ground")
                    out.append(_violation(
                        "si_termination", "warning", [loc], [net],
                        f"net {net!r} (class {cls_name!r}) requires a THEVENIN termination "
                        f"but is missing: {', '.join(missing)}"))
            else:
                out.append(_violation(
                    "si_termination", "warning", [loc], [net],
                    f"net {net!r}: unknown termination scheme {scheme!r} in class "
                    f"{cls_name!r} (want series | parallel | thevenin | none)"))
    return out


def _other(r: dict, net: str) -> str:
    a, b = r["nets"]
    return b if a == net else a


# ────────────────────────────────────────────────────── via stubs


def _copper_z(board: dict) -> dict[str, float]:
    """z-depth (mm from the top surface) of each copper layer from the physical stackup."""
    z: dict[str, float] = {}
    depth = 0.0
    for ly in board.get("stackup", {}).get("layers", []):
        th = float(ly.get("thickness", 0.0) or 0.0)
        if ly.get("type") == "copper":
            z[str(ly.get("id"))] = depth
        depth += th
    if not z:  # no explicit stackup — assume a 1.6 mm 2-layer board
        z = {"F.Cu": 0.0, "B.Cu": 1.6}
    return z


def check_via_stubs(board: dict) -> list[dict]:
    """Via-stub length vs signal rise time for every net in a class with ``rise_time_ps``.

    For each through/blind/buried via on such a net, the layers the net's tracks actually use
    define the signal path; barrel beyond the outermost used layer is a stub. Heuristic: warn
    when the ROUND-TRIP stub delay ``2 * stub_mm * delay_ps_per_mm`` exceeds ``rise_time / 3``
    (the classic tr/3 reflection rule of thumb). Uses the thickness-weighted stripline er of the
    via's layer span for the delay.
    """
    out: list[dict] = []
    classes = board.get("net_classes", {})
    by_class = _class_nets(board)
    zmap = _copper_z(board)
    if not zmap:
        return out

    # net -> set of copper layers its tracks use
    net_layers: dict[str, set[str]] = {}
    for tr in board.get("tracks", []):
        if tr.get("net"):
            net_layers.setdefault(tr["net"], set()).add(str(tr.get("layer", "F.Cu")))

    for cls_name in sorted(classes):
        cls = classes[cls_name] or {}
        tr_ps = cls.get("rise_time_ps")
        if not tr_ps:
            continue
        tr_ps = float(tr_ps)
        budget_ps = tr_ps / 3.0
        for net in by_class.get(cls_name, []):
            used = net_layers.get(net, set())
            used_z = sorted(zmap[l] for l in used if l in zmap)
            if not used_z:
                continue
            for v in board.get("vias", []):
                if v.get("net") != net:
                    continue
                span = [l for l in (v.get("layers") or ["F.Cu", "B.Cu"]) if l in zmap]
                if len(span) < 2:
                    span = ["F.Cu", "B.Cu"]
                z0 = min(zmap[l] for l in span)
                z1 = max(zmap[l] for l in span)
                # stub = barrel beyond the outermost track layers the signal actually uses
                stub_mm = max(used_z[0] - z0, z1 - used_z[-1], 0.0)
                if stub_mm <= 1e-6:
                    continue
                # delay through the barrel: use the board's average er (stripline-ish field)
                geo = _stackup_geometry(board, "F.Cu")
                er = float(geo.get("er", 4.3))
                delay_ps = _delay_ps_per_mm(er) * stub_mm
                rt_ps = 2.0 * delay_ps
                if rt_ps > budget_ps + 1e-9:
                    at = v.get("at", [0.0, 0.0])
                    out.append(_violation(
                        "si_via_stub", "warning",
                        [(float(at[0]), float(at[1]), "F.Cu")], [v.get("id", ""), net],
                        f"via {v.get('id', '')} on net {net!r} has a {stub_mm:.2f}mm stub "
                        f"(round-trip {rt_ps:.0f}ps > tr/3 = {budget_ps:.0f}ps for "
                        f"tr={tr_ps:.0f}ps) — use the full barrel, move to a layer pair "
                        f"nearer the stub end, or back-drill"))
    return out


# ────────────────────────────────────────────────────── BGA fanout


def _bga_footprints(board: dict, min_pads: int = 36) -> list[dict]:
    """Footprints treated as BGAs: fpid/name mentions 'bga' OR >= min_pads SMD pads."""
    out = []
    for fp in board.get("footprints", []):
        name = f"{fp.get('fpid', '')} {fp.get('name', '')}".lower()
        smd = [p for p in fp.get("pads", []) if p.get("type", "smd") == "smd"]
        if "bga" in name or len(smd) >= min_pads:
            out.append(fp)
    return out


def _pad_radius(pad: dict) -> float:
    size = pad.get("size", [0.0, 0.0]) or [0.0, 0.0]
    w = float(size[0]) if size else 0.0
    h = float(size[1]) if len(size) > 1 else w
    return max(w, h) / 2.0


def check_bga_fanout(board: dict) -> list[dict]:
    """BGA fanout heuristics: via-in-pad, escape-channel feasibility, courtyard congestion."""
    out: list[dict] = []
    vias = board.get("vias", [])
    rules = board.get("rules", {})
    clearance = float(rules.get("clearance", 0.2))
    track_w = float(rules.get("track_width_min", 0.15))

    for fp in _bga_footprints(board):
        fid = fp.get("id", "")
        ref = fp.get("ref", fid)
        pads = [p for p in fp.get("pads", []) if p.get("type", "smd") == "smd"]
        if not pads:
            continue
        world = {p.get("id", str(i)): schema.pad_world(fp, p) for i, p in enumerate(pads)}
        xs = [w[0] for w in world.values()]
        ys = [w[1] for w in world.values()]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        pad_r = max(_pad_radius(p) for p in pads)

        # ── via-in-pad: via centre inside a pad's copper circle ──
        for v in vias:
            at = v.get("at", [0.0, 0.0])
            vx, vy = float(at[0]), float(at[1])
            for p in pads:
                wx, wy = schema.pad_world(fp, p)
                if math.hypot(vx - wx, vy - wy) <= _pad_radius(p) + 1e-9:
                    out.append(_violation(
                        "si_bga_via_in_pad", "warning", [(vx, vy, "F.Cu")],
                        [v.get("id", ""), f"{fid}:{p.get('id', '')}"],
                        f"via {v.get('id', '')} is inside BGA {ref} pad {p.get('id', '')} "
                        f"(via-in-pad) — requires filled+capped vias at fab, or dog-bone "
                        f"fanout instead"))
                    break

        # ── escape feasibility: channel between adjacent balls vs one track + clearances ──
        pitch = _min_pitch(list(world.values()))
        if pitch is not None:
            channel = pitch - 2.0 * pad_r
            need = track_w + 2.0 * clearance
            rings = _ring_depth(list(world.values()), pitch)
            if rings > 2 and channel < need - 1e-9:
                cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
                out.append(_violation(
                    "si_bga_escape", "warning", [(cx, cy, "F.Cu")], [fid],
                    f"BGA {ref}: escape channel {channel:.3f}mm between balls "
                    f"(pitch {pitch:.3f}mm, pad Ø{2 * pad_r:.3f}mm) < track+2×clearance "
                    f"{need:.3f}mm and inner rings ({rings}) need routed escapes — "
                    f"narrow tracks/clearance under the BGA or use via-in-pad fanout"))

        # ── courtyard congestion: copper occupancy under the ball field ──
        area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 1e-9)
        pad_area = sum(math.pi * _pad_radius(p) ** 2 for p in pads)
        via_area = 0.0
        n_vias = 0
        for v in vias:
            at = v.get("at", [0.0, 0.0])
            if bbox[0] - pad_r <= float(at[0]) <= bbox[2] + pad_r and \
               bbox[1] - pad_r <= float(at[1]) <= bbox[3] + pad_r:
                via_area += math.pi * (float(v.get("diameter", 0.0)) / 2.0) ** 2
                n_vias += 1
        occupancy = (pad_area + via_area) / area
        if occupancy > 0.5:
            cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
            out.append(_violation(
                "si_bga_congestion", "warning", [(cx, cy, "F.Cu")], [fid],
                f"BGA {ref}: courtyard congestion {occupancy:.0%} copper occupancy under "
                f"the ball field ({len(pads)} pads + {n_vias} vias) > 50% — routing "
                f"channels are nearly closed; spread fanout vias or add layers"))
    return out


def _min_pitch(points: list[tuple[float, float]]) -> float | None:
    """Smallest non-zero pairwise distance (the ball pitch of a regular grid)."""
    best = None
    pts = sorted(points)
    for i in range(len(pts)):
        for j in range(i + 1, min(i + 12, len(pts))):  # neighbours only — sorted sweep
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            if d > 1e-6 and (best is None or d < best):
                best = d
    return best


def _ring_depth(points: list[tuple[float, float]], pitch: float) -> int:
    """How many rings deep the ball grid is (inner balls need routed/via escapes)."""
    if not points or pitch <= 0:
        return 0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    nx = round((max(xs) - min(xs)) / pitch) + 1
    ny = round((max(ys) - min(ys)) / pitch) + 1
    return (min(nx, ny) + 1) // 2


# ────────────────────────────────────────────────────── declaration helper


def set_net_class(board: dict, name: str, params: dict | None = None,
                  nets: list[str] | None = None) -> dict:
    """Create/extend a net class with SI keys and assign nets to it (mutates ``board``).

    ``params`` keys are merged ADDITIVELY into ``board['net_classes'][name]`` — this is the
    same schema stackup.apply_solution writes (track_width/diff_gap/via_*), extended with the
    SI keys documented in this module (match_tolerance_mm, pair_skew_mm, termination,
    rise_time_ps). Unknown keys are stored as-is (forward-compatible).

    ``nets`` are assigned to the class (created in ``board['nets']`` if new — same behaviour
    as stackup.apply_solution's net_map).

    Returns ``{"class": name, "params": <merged entry>, "assigned": [nets...]}``.
    """
    ncs = board.setdefault("net_classes", {})
    entry = dict(ncs.get(name, {}))
    for k, v in (params or {}).items():
        entry[k] = v
    ncs[name] = entry

    assigned: list[str] = []
    if nets:
        known = {n.get("name"): n for n in board.get("nets", []) if isinstance(n, dict)}
        for net in nets:
            if net in known:
                known[net]["class"] = name
            else:
                board.setdefault("nets", []).append({"name": net, "class": name})
            assigned.append(net)
    return {"class": name, "params": entry, "assigned": assigned}


# ────────────────────────────────────────────────────── entry point


def run_sicheck(board: dict) -> dict:
    """Run the full SI rule pack over a board document.

    Pure + advisory (mirrors :func:`services.ecad.drc.run_drc`): never mutates the board, never
    raises on a bad board, deterministic sorted output.

    Returns::

        {"violations": [Violation...],      # PCB-contract shape, sorted
         "match_report": {group: {...per-net length/delta table...}},
         "limitations": [str, ...]}         # honest scope statement — surface to the user
    """
    if not schema.is_board(board):
        return {"violations": [], "match_report": {}, "limitations": list(LIMITATIONS)}

    violations: list[dict] = []
    match_v, match_report = check_match_groups(board)
    violations.extend(match_v)
    violations.extend(check_termination(board))
    violations.extend(check_via_stubs(board))
    violations.extend(check_bga_fanout(board))

    violations.sort(key=lambda v: (v["rule"], v["severity"], tuple(v["items"]),
                                   tuple(tuple(loc) for loc in v["locs"]), v["message"]))
    return {"violations": violations, "match_report": match_report,
            "limitations": list(LIMITATIONS)}


if __name__ == "__main__":  # pragma: no cover - smoke
    import json
    import sys
    doc = json.load(open(sys.argv[1])) if len(sys.argv) > 1 else schema.new_board()
    print(json.dumps(run_sicheck(doc), indent=2, sort_keys=True))
